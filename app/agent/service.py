import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

from pydantic import ValidationError

from app.agent.tools import TOOLS
from app.conversations.service import ConversationService
from app.llm.protocol import LLMProvider
from app.memories.service import MemoryService
from app.models import ConversationMessageRole, DraftStatus, RecurrenceFrequency
from app.reminders.canceling import ReminderCanceler
from app.reminders.errors import DuplicateReminderError, ReminderInPastError
from app.reminders.rescheduling import ReminderRescheduler
from app.reminders.snoozing import ReminderSnoozer
from app.reminders.serialization import to_response
from app.reminders.service import ReminderService
from app.schemas import (
    CancellationScope,
    ReminderCancelRequest,
    ReminderCreate,
    ReminderQuery,
    ReminderRescheduleRequest,
    RescheduleScope,
    SnoozeRequest,
)
from app.time_utils import to_user_timezone
from app.users.service import UserService

SYSTEM_PROMPT = """You are Memoray, a reminder and memory assistant. You create, list, reschedule, cancel, and snooze reminders, and save or retrieve durable user memories.

**Creation:** Require title, date, and time; recurring reminders also require frequency (daily, weekly, monthly, yearly)
and optional interval. Use the draft tool for missing fields and `create_reminders` for multiple complete requests. Never
invent values. Resolve relative times using the user's timezone and current time.

**Rescheduling/Snoozing:** `reschedule_reminder` is mutation-only; requires exact `reminder_id`, resolved future schedule,
and scope. Never use it for discovery. Unless an exact ID is provided, first call `list_reminders` with relevant title
substring and `status=pending`; if not uniquely identified, retry once without title. Never mutate ambiguous matches. For
recurring reminders, use `scope=occurrence` with exact pending `occurrence_id` for a specific date; use `scope=series` only
for explicit series-wide changes. Clarify ambiguity. Snoozing requires a delivered occurrence and positive duration.

**Cancellation:** `cancel_reminder` is mutation-only. Always discover the exact `reminder_id` via `list_reminders`, first
filtering by title and `status=pending`, then at most once without title. Use `scope=one_time` for non-recurring reminders,
`scope=occurrence` with exact pending `occurrence_id` to skip one instance, or `scope=series` only when explicitly requested.
Never cancel ambiguous or unmatched reminders; clarify or report none found.

**Memories:** A memory is a durable fact attached to this user, not a reminder and never a scheduled task. Call
`create_memory` only after the user explicitly asks you to remember one clear, complete fact. If the request or fact is
unclear, ask a clarification question and do not save anything. Call `retrieve_memories` before answering a question
about a remembered fact. Use only returned candidates. If no candidate matches, say that you do not have a matching
memory. If multiple candidates match, ask which one the user means instead of guessing.

**Tool safety:** Call each mutation once after discovery. Never re-fetch to verify success or repeat successful mutations.
Do not guess IDs, schedules, or scope.

**Responses:** For unrelated requests, state that you only manage reminders and memories. After successful tool results, respond warmly
in ≤25 words using only the returned information.
"""


@dataclass
class AgentResult:
    conversation_id: uuid.UUID
    response: str
    draft_status: str | None


class ReminderAgent:
    def __init__(
        self,
        provider: LLMProvider,
        reminder_service: ReminderService,
        user_service: UserService,
        conversation_service: ConversationService,
        memory_service: MemoryService | None = None,
    ):
        self.provider = provider
        self.reminders = reminder_service
        self.users = user_service
        self.conversations = conversation_service
        self.memories = memory_service

    def handle(
        self,
        user_id: uuid.UUID,
        message: str,
        conversation_id: uuid.UUID | None = None,
    ) -> AgentResult:
        user = self.users.get(user_id)
        conversation = self.conversations.get_or_create(user_id, conversation_id)
        self.conversations.record_message(conversation.id, ConversationMessageRole.user, message)

        history = self.conversations.recent_messages(conversation, limit=4)
        if not history or history[-1].get("content") != message:
            history.append({"role": "user", "content": message})
        draft = conversation.draft
        draft_context = self._draft_context(draft)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "system",
                "content": f"Active reminder draft: {json.dumps(draft_context)}",
            },
            {
                "role": "system",
                "content": (
                    f"User timezone: {user.timezone}\n"
                    f"Current time: {to_user_timezone(datetime.now(timezone.utc), user.timezone).isoformat()}"
                ),
            },
            *history,
        ]
        final_response = None
        last_tool_results: list[dict[str, Any]] = []
        working_messages = messages
        reschedule_workflow = self._looks_like_reschedule_request(message)
        cancellation_workflow = self._looks_like_cancellation_request(message)
        discovery_calls = 0
        mutation_attempted = False
        last_discovery_result: dict[str, Any] | None = None
        memory_retrieval_result: dict[str, Any] | None = None
        for _ in range(4):
            response = self.provider.complete(working_messages, TOOLS)
            if not response.tool_calls:
                final_response = response
                break
            last_tool_results = []
            for call in response.tool_calls:
                if call.name == "reschedule_reminder":
                    reschedule_workflow = True
                if call.name == "cancel_reminder":
                    cancellation_workflow = True
                mutation_workflow = reschedule_workflow or cancellation_workflow
                if call.name == "list_reminders" and mutation_workflow:
                    discovery_calls += 1
                    if discovery_calls > 2:
                        last_tool_results.append(
                            {
                                "operation": "discovery_rejected",
                                "error": "Reminder discovery is limited to one filtered search and one broader fallback.",
                            }
                        )
                        continue
                    if discovery_calls == 2 and call.arguments.get("title"):
                        last_tool_results.append(
                            {
                                "operation": "discovery_rejected",
                                "error": "The second discovery call must be the broader listing without a title filter.",
                            }
                        )
                        continue
                if call.name in {"reschedule_reminder", "cancel_reminder"}:
                    if mutation_attempted:
                        last_tool_results.append(
                            {
                                "operation": "rejected",
                                "error": "A reminder mutation was already attempted; do not retry it.",
                            }
                        )
                        continue
                    if call.name == "cancel_reminder" and last_discovery_result is None:
                        last_tool_results.append(
                            {
                                "operation": "discovery_required",
                                "error": "Use list_reminders before mutating a reminder.",
                            }
                        )
                        mutation_attempted = True
                        continue
                    if mutation_workflow and last_discovery_result is not None:
                        candidates = last_discovery_result.get("reminders", [])
                        reminder_id = call.arguments.get("reminder_id")
                        if len(candidates) != 1:
                            last_tool_results.append(
                                {
                                    "operation": "target_ambiguous" if candidates else "target_not_found",
                                    "count": len(candidates),
                                    "error": "Please clarify which reminder you mean."
                                    if candidates
                                    else "No suitable active reminder found.",
                                }
                            )
                            mutation_attempted = True
                            continue
                        if reminder_id != candidates[0].get("id"):
                            last_tool_results.append(
                                {
                                    "operation": "target_not_discovered",
                                    "error": "Use the reminder_id returned by list_reminders; do not guess IDs.",
                                }
                            )
                            mutation_attempted = True
                            continue
                        if call.name == "cancel_reminder" and call.arguments.get("scope") == "occurrence":
                            occurrence_id = call.arguments.get("occurrence_id")
                            discovered_occurrence = (candidates[0].get("occurrence") or {}).get("id")
                            if not occurrence_id or occurrence_id != discovered_occurrence:
                                last_tool_results.append(
                                    {
                                        "operation": "target_not_discovered",
                                        "error": "Use the occurrence_id returned by list_reminders for the requested occurrence.",
                                    }
                                )
                                mutation_attempted = True
                                continue
                    mutation_attempted = True
                result = self._execute_tool(user_id, conversation, call)
                last_tool_results.append(result)
                if call.name == "retrieve_memories":
                    memory_retrieval_result = result
                if call.name == "list_reminders" and mutation_workflow:
                    last_discovery_result = result
            final_context = {
                "role": "user",
                "content": (
                    "Tool results are below. Follow the discovery-before-mutation rules in the system "
                    "instructions. Otherwise write only the final conversational response:\n"
                    f"{json.dumps(last_tool_results)}"
                ),
            }
            working_messages = working_messages + [final_context]

            if mutation_workflow and any(call.name == "list_reminders" for call in response.tool_calls) and not any(
                call.name in {"reschedule_reminder", "cancel_reminder"} for call in response.tool_calls
            ):
                discovery_result = last_discovery_result or {}
                if discovery_result.get("count") == 1 or (
                    discovery_calls == 1 and discovery_result.get("count") == 0
                ):
                    continue
            final_response = self.provider.complete(
                [
                    *working_messages[:-1],
                    {
                        "role": "system",
                        "content": "Final response mode: do not call tools; write only the answer.",
                    },
                    working_messages[-1],
                ],
                [],
            )
            break
        if final_response is None and memory_retrieval_result is None:
            raise RuntimeError("LLM did not return a final response after reminder tool calls")
        if not final_response.text and memory_retrieval_result is None:
            raise RuntimeError("LLM did not return a final response after the reminder tool call")
        if memory_retrieval_result is not None:
            text = self._ground_memory_response(memory_retrieval_result)
        else:
            text = self._limit_words(final_response.text)
        self.conversations.record_message(conversation.id, ConversationMessageRole.assistant, text)
        draft_operations = {"draft_updated", "draft_not_updated", "created", "created_batch", "not_created"}
        draft_status = (
            conversation.draft.status.value
            if any(result.get("operation") in draft_operations for result in last_tool_results)
            else None
        )
        return AgentResult(conversation.id, text, draft_status)

    def _execute_tool(self, user_id: uuid.UUID, conversation, call) -> dict[str, Any]:
        if call.name == "update_reminder_draft":
            result = self._update_draft(conversation, call.arguments)
            if result.get("draft_status") == DraftStatus.ready.value:
                return self._create_from_draft(user_id, conversation)
            return result
        if call.name == "create_reminder":
            self._update_draft(conversation, call.arguments)
            return self._create_from_draft(user_id, conversation)
        if call.name == "create_reminders":
            return self._create_many(user_id, conversation, call.arguments)
        if call.name == "list_reminders":
            return self._list(user_id, call.arguments)
        if call.name == "reschedule_reminder":
            return self._reschedule(user_id, call.arguments)
        if call.name == "cancel_reminder":
            return self._cancel(user_id, call.arguments)
        if call.name == "snooze_occurrence":
            return self._snooze(user_id, call.arguments)
        if call.name == "create_memory":
            return self._create_memory(user_id, call.arguments)
        if call.name == "retrieve_memories":
            return self._retrieve_memories(user_id, call.arguments)
        return {"operation": "rejected", "error": "unsupported reminder operation"}

    def _create_memory(self, user_id: uuid.UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.memories is None:
            return {"operation": "memory_not_created", "error": "Memory storage is not configured."}
        try:
            memory = self.memories.create(user_id, arguments.get("content", ""))
        except (TypeError, ValueError, LookupError) as exc:
            return {"operation": "memory_not_created", "error": str(exc)}
        return {
            "operation": "memory_created",
            "memory": {
                "id": str(memory.id),
                "content": memory.content,
                "embedding_status": memory.embedding_status.value,
            },
        }

    def _retrieve_memories(self, user_id: uuid.UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.memories is None:
            return {"operation": "memory_not_retrieved", "error": "Memory retrieval is not configured."}
        if self.memories.embedding_provider is None:
            return {
                "operation": "memory_not_retrieved",
                "error": "Memory embedding provider is not configured.",
                "candidates": [],
            }
        try:
            candidates = self.memories.retrieve(user_id, arguments.get("query", ""))
        except (TypeError, ValueError, LookupError) as exc:
            return {"operation": "memory_not_retrieved", "error": str(exc), "candidates": []}
        return {
            "operation": "memories_retrieved",
            "count": len(candidates),
            "candidates": candidates,
        }

    @staticmethod
    def _ground_memory_response(result: dict[str, Any]) -> str:
        candidates = result.get("candidates") or []
        if result.get("operation") != "memories_retrieved":
            return "I couldn't search your memories right now."
        if not candidates:
            return "I don't have a matching memory."
        if len(candidates) > 1:
            facts = "; ".join(str(item.get("content", "")) for item in candidates)
            return f"I found multiple matching memories: {facts}. Which one did you mean?"
        return f"I remember: {candidates[0].get('content', '')}"

    def _update_draft(self, conversation, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            schedule_date = date.fromisoformat(arguments["date"]) if arguments.get("date") else None
            schedule_time = time.fromisoformat(arguments["time"]) if arguments.get("time") else None
            frequency = (
                RecurrenceFrequency(arguments["recurrence_frequency"])
                if arguments.get("recurrence_frequency")
                else None
            )
            recurrence_interval = (
                int(arguments["recurrence_interval"])
                if arguments.get("recurrence_interval") is not None
                else None
            )
            draft = self.conversations.update_draft(
                conversation.draft,
                title=arguments.get("title"),
                schedule_date=schedule_date,
                schedule_time=schedule_time,
                is_recurring=arguments.get("recurring"),
                recurrence_frequency=frequency,
                recurrence_interval=recurrence_interval,
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            return {"operation": "draft_not_updated", "error": "One or more supplied fields are invalid."}
        missing = self.conversations.missing_fields(draft)
        return {
            "operation": "draft_updated",
            "draft_status": draft.status.value,
            "missing_fields": missing,
            "draft": self._draft_context(draft),
        }

    def _reschedule(self, user_id: uuid.UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            user = self.users.get(user_id)
            reminder_id = arguments.get("reminder_id")
            if not reminder_id:
                return {
                    "operation": "discovery_required",
                    "error": "An exact reminder_id is required; use list_reminders for discovery.",
                }
            schedule_value = arguments.get("schedule")
            if not schedule_value or not arguments.get("scope"):
                return {"operation": "not_rescheduled", "error": "reminder_id, schedule, and scope are required."}
            schedule = datetime.fromisoformat(schedule_value)
            request = ReminderRescheduleRequest(
                schedule=schedule,
                scope=RescheduleScope(arguments["scope"]),
                occurrence_id=uuid.UUID(arguments["occurrence_id"])
                if arguments.get("occurrence_id")
                else None,
            )
            reminder = ReminderRescheduler(self.reminders.db).reschedule(
                user_id, uuid.UUID(reminder_id), request
            )
        except (KeyError, TypeError, ValueError, ValidationError, LookupError) as exc:
            return {"operation": "not_rescheduled", "error": str(exc)}
        user = self.users.get(user_id)
        return {
            "operation": "rescheduled",
            "reminder": to_response(reminder, user.timezone).model_dump(mode="json"),
        }

    @staticmethod
    def _looks_like_reschedule_request(message: str) -> bool:
        words = message.casefold()
        return any(term in words for term in ("reschedule", "move my", "move the", "postpone", "push back", "shift"))

    @staticmethod
    def _looks_like_cancellation_request(message: str) -> bool:
        words = message.casefold()
        return any(
            term in words
            for term in ("cancel", "cancellation", "delete", "deletion", "stop", "skip")
        )

    def _cancel(self, user_id: uuid.UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            reminder_id = arguments.get("reminder_id")
            scope = arguments.get("scope")
            if not reminder_id or not scope:
                return {
                    "operation": "discovery_required",
                    "error": "An exact reminder_id and cancellation scope are required; use list_reminders for discovery.",
                }
            request = ReminderCancelRequest(
                scope=CancellationScope(scope),
                occurrence_id=uuid.UUID(arguments["occurrence_id"])
                if arguments.get("occurrence_id")
                else None,
            )
            reminder = ReminderCanceler(self.reminders.db).cancel(
                user_id, uuid.UUID(reminder_id), request
            )
        except (KeyError, TypeError, ValueError, ValidationError, LookupError) as exc:
            return {"operation": "not_canceled", "error": str(exc)}
        user = self.users.get(user_id)
        return {
            "operation": "canceled",
            "scope": request.scope.value,
            "reminder": to_response(reminder, user.timezone).model_dump(mode="json"),
        }

    def _snooze(self, user_id: uuid.UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            reminder_id = arguments.get("reminder_id")
            occurrence_id = arguments.get("occurrence_id")
            if not reminder_id:
                title = arguments.get("title")
                if not title:
                    return {"operation": "not_snoozed", "error": "A reminder title or ID is required."}
                candidates = [
                    item
                    for item in self.reminders.list(user_id, ReminderQuery(title=title))
                    if item.status.value == "active"
                ]
                if len(candidates) != 1:
                    return {
                        "operation": "target_ambiguous" if candidates else "target_not_found",
                        "count": len(candidates),
                        "error": "Please clarify which reminder you mean."
                        if candidates
                        else "No matching active reminder found.",
                    }
                reminder_id = str(candidates[0].id)
                notified = [item for item in candidates[0].occurrences if item.status.value == "notified"]
                if len(notified) != 1:
                    return {
                        "operation": "target_ambiguous" if notified else "not_snoozable",
                        "count": len(notified),
                        "error": "Exactly one notified occurrence is required to snooze.",
                    }
                occurrence_id = str(notified[0].id)
            if not occurrence_id:
                return {"operation": "not_snoozed", "error": "An occurrence ID is required."}
            request = SnoozeRequest(duration_seconds=arguments["duration_seconds"])
            reminder = ReminderSnoozer(self.reminders.db).snooze(
                user_id,
                uuid.UUID(reminder_id),
                uuid.UUID(occurrence_id),
                request,
            )
        except (KeyError, TypeError, ValueError, ValidationError, LookupError) as exc:
            return {"operation": "not_snoozed", "error": str(exc)}
        user = self.users.get(user_id)
        return {
            "operation": "snoozed",
            "reminder": to_response(reminder, user.timezone).model_dump(mode="json"),
        }

    def _create_from_draft(self, user_id: uuid.UUID, conversation) -> dict[str, Any]:
        draft = conversation.draft
        missing = self.conversations.missing_fields(draft)
        if missing:
            return {
                "operation": "not_created",
                "draft_status": draft.status.value,
                "missing_fields": missing,
            }
        try:
            reminder = self.reminders.create_from_local_parts(
                user_id,
                title=draft.title,
                schedule_date=draft.schedule_date,
                schedule_time=draft.schedule_time,
                recurrence_frequency=draft.recurrence_frequency if draft.is_recurring else None,
                recurrence_interval=draft.recurrence_interval,
            )
        except ReminderInPastError:
            return {"operation": "not_created", "error": "The reminder time must be strictly in the future."}
        except DuplicateReminderError:
            return {"operation": "not_created", "error": "An equivalent active reminder already exists."}
        self.conversations.mark_created(draft, conversation)
        user = self.users.get(user_id)
        return {
            "operation": "created",
            "reminder": to_response(reminder, user.timezone).model_dump(mode="json"),
        }

    def _create_many(
        self, user_id: uuid.UUID, conversation, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        created = []
        rejected = []
        user = self.users.get(user_id)
        for item in arguments.get("reminders", []):
            try:
                schedule_date = date.fromisoformat(item["date"])
                schedule_time = time.fromisoformat(item["time"])
                frequency = (
                    RecurrenceFrequency(item["recurrence_frequency"])
                    if item.get("recurrence_frequency")
                    else None
                )
                recurrence_interval = int(item.get("recurrence_interval", 1))
                if item.get("recurring") and frequency is None:
                    raise ValueError("recurrence frequency is required for recurring reminders")
                reminder = self.reminders.create_from_local_parts(
                    user_id,
                    title=item["title"],
                    schedule_date=schedule_date,
                    schedule_time=schedule_time,
                    recurrence_frequency=frequency,
                    recurrence_interval=recurrence_interval,
                )
                created.append(to_response(reminder, user.timezone).model_dump(mode="json"))
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                rejected.append({"title": item.get("title"), "error": str(exc)})
            except (ReminderInPastError, DuplicateReminderError) as exc:
                rejected.append({"title": item.get("title"), "error": str(exc)})
        if created and conversation.draft:
            self.conversations.mark_created(conversation.draft, conversation)
        return {
            "operation": "created_batch",
            "created_count": len(created),
            "created": created,
            "rejected": rejected,
        }

    def _list(self, user_id: uuid.UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            query = ReminderQuery(
                title=arguments.get("title"),
                start=self._parse_optional(arguments.get("start")),
                end=self._parse_optional(arguments.get("end"), end=True),
                occurrence_start=self._parse_optional(arguments.get("occurrence_start")),
                occurrence_end=self._parse_optional(arguments.get("occurrence_end"), end=True),
                status=arguments.get("status"),
            )
        except (ValueError, ValidationError):
            return {"operation": "not_retrieved", "error": "The reminder filter was invalid."}
        user = self.users.get(user_id)
        reminders = self.reminders.list_with_occurrences(user_id, query)
        return {
            "operation": "retrieved",
            "count": len(reminders),
            "reminders": [
                to_response(item, user.timezone, occurrence).model_dump(mode="json")
                for item, occurrence in reminders
            ],
        }

    @staticmethod
    def _draft_context(draft) -> dict[str, Any]:
        return {
            "title": draft.title,
            "date": draft.schedule_date.isoformat() if draft.schedule_date else None,
            "time": draft.schedule_time.isoformat() if draft.schedule_time else None,
            "recurring": draft.is_recurring,
            "recurrence_frequency": draft.recurrence_frequency.value
            if draft.recurrence_frequency
            else None,
            "recurrence_interval": draft.recurrence_interval,
            "status": draft.status.value,
        }

    @staticmethod
    def _parse_optional(value: str | None, *, end: bool = False):
        if not value:
            return None
        if len(value) == 10:
            parsed_date = date.fromisoformat(value)
            return datetime.combine(parsed_date, time.max if end else time.min)
        return datetime.fromisoformat(value)

    @staticmethod
    def _limit_words(text: str, limit: int = 25) -> str:
        return " ".join(text.split()[:limit])
