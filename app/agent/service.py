import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

from pydantic import ValidationError

from app.agent.tools import TOOLS
from app.conversations.service import ConversationService
from app.llm.protocol import LLMProvider, LLMResponse
from app.memories.service import MemoryService
from app.models import ChannelIdentity, ConversationMessageRole, DraftStatus, RecurrenceFrequency
from app.reminders.canceling import ReminderCanceler
from app.reminders.completion import CompletionService
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
from app.time_utils import format_user_datetime, to_user_timezone
from app.users.service import UserService

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Memoray, a reminder and memory assistant. You create, list, reschedule, cancel, and snooze reminders, and save or retrieve durable user memories.

**Channels:** Memoray is a super connector: reminders and memories are shared across connected Telegram, Discord, Vapi
phone calls, and ChatGPT/MCP clients. Google Calendar only mirrors reminders; Memoray is the source of truth. For setup
questions, or if Telegram is unconnected, show: Telegram `/start`; phone/Vapi and urgent calls: Telegram `/link phone`,
then call +1 878 284 2928; Discord `/connect_discord`; Google Calendar `/connect_calendar`; ChatGPT/MCP: add Memoray's
MCP server, then approve its displayed code in Telegram with `/connect_chatgpt <approval-code>`. Otherwise omit this guide.

**Create:** Require title, date, and time; recurring reminders need frequency. Use the draft for missing fields and
`create_reminders` for multiple complete requests. Never invent values; resolve relative times from supplied timezone and
current time. Mark urgent only when explicit: call the linked phone twice, then escalate on Telegram if unanswered.

**Complete:** For Done, finished, or delivery confirmation, call `complete_delivered_occurrence`, including any stated
title. Complete one occurrence only; clarify ambiguity and report repeats as already completed.

**Reschedule/snooze:** Rescheduling is mutation-only and needs an exact ID, future schedule, and scope. Without an exact ID,
list pending reminders by title, then at most once without title; never mutate an ambiguous match. For one recurring instance,
use `occurrence` and its pending occurrence ID; use `series` for a non-recurring reminder or explicit series-wide change.
Snoozing needs one delivered occurrence and a positive duration.

**Cancel:** This is mutation-only. Discover the exact ID by listing pending reminders by title, then at most once without
title. Use `one_time` for non-recurring, `occurrence` with its pending ID for one instance, or `series` only when explicit.
Never cancel ambiguous or unmatched reminders.

**Memory:** A memory is a durable user fact, never a task. Save only clear, explicit requests; otherwise clarify. Retrieve
before answering and use only returned candidates; say none match or clarify multiple matches.

**Safety:** Mutate once after discovery; never re-fetch to verify. Do not guess IDs, schedules, or scope; copy discovered
UUIDs exactly.

**Responses:** For unrelated requests, say you manage reminders and memories. When listing, use the human-readable display
fields, never raw timestamps or IDs. After successful tool results, respond warmly
in ≤25 words using only the returned information. Reply in conversational, humanized way without emojis
"""

INTERNAL_ID_PATTERN = re.compile(
    r"(?i)\b(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})\b"
)


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
        notification_identity: ChannelIdentity | None = None,
    ):
        self.provider = provider
        self.reminders = reminder_service
        self.users = user_service
        self.conversations = conversation_service
        self.memories = memory_service
        self.notification_identity = notification_identity

    def handle(
        self,
        user_id: uuid.UUID,
        message: str,
        conversation_id: uuid.UUID | None = None,
        *,
        commit: bool = True,
    ) -> AgentResult:
        user = self.users.get(user_id)
        conversation = self.conversations.get_or_create(user_id, conversation_id, commit=commit)
        self.conversations.record_message(
            conversation.id, ConversationMessageRole.user, message, commit=commit
        )

        history = self.conversations.recent_messages(conversation, limit=4)
        history = [
            {
                **item,
                "content": self._redact_internal_ids(item["content"])
                if item.get("role") == "assistant"
                else item["content"],
            }
            for item in history
        ]
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
                result = self._execute_tool(user_id, conversation, call, commit=commit)
                last_tool_results.append(result)
                if call.name == "retrieve_memories":
                    memory_retrieval_result = result
                if call.name == "list_reminders" and mutation_workflow:
                    last_discovery_result = result
            if any(
                call.name == "complete_delivered_occurrence"
                for call in response.tool_calls
            ):
                # Completion replies are deterministic and must not depend on
                # a second model turn that could reinterpret the mutation.
                final_response = LLMResponse(text="")
                break
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
            # A model may use an empty discovery result as a planning step
            # before creating a newly requested reminder. Give it another
            # tool-enabled turn instead of treating the next tool call as a
            # final text response.
            if any(call.name == "list_reminders" for call in response.tool_calls):
                discovery_result = last_tool_results[-1] if last_tool_results else {}
                if discovery_result.get("operation") == "retrieved" and discovery_result.get("count") == 0:
                    continue
            redacted_final_context = {
                **working_messages[-1],
                "content": self._redact_internal_ids(working_messages[-1]["content"]),
            }
            final_response = self.provider.complete(
                [
                    *self._redact_messages(working_messages[:-1]),
                    {
                        "role": "system",
                        "content": (
                            "Final response mode: do not call tools; write only the answer. "
                            "Tool results are authoritative and have already been executed. "
                            "If the results contain operation=rescheduled, created, created_batch, "
                            "canceled, or snoozed, confirm that it completed in the past tense. "
                            "Never say that a tool call is still needed or pending."
                        ),
                    },
                    redacted_final_context,
                ],
                [],
            )
            break
        completion_results = [
            result
            for result in last_tool_results
            if result.get("operation")
            in {"completed", "already_completed", "ambiguous", "not_found", "not_delivered"}
        ]
        if final_response is None and memory_retrieval_result is None and not completion_results:
            raise RuntimeError("LLM did not return a final response after reminder tool calls")
        if (
            final_response is not None
            and not final_response.text
            and memory_retrieval_result is None
            and not completion_results
        ):
            raise RuntimeError("LLM did not return a final response after the reminder tool call")
        if completion_results:
            text = self._completion_response(completion_results[-1])
        elif memory_retrieval_result is not None:
            text = self._ground_memory_response(memory_retrieval_result)
        else:
            grounded_text = self._ground_successful_mutation_response(
                final_response.text, last_tool_results
            )
            text = self._redact_internal_ids(self._limit_words(grounded_text))
        self.conversations.record_message(
            conversation.id, ConversationMessageRole.assistant, text, commit=commit
        )
        if commit:
            self.reminders.db.commit()
        draft_operations = {"draft_updated", "draft_not_updated", "created", "created_batch", "not_created"}
        draft_status = (
            conversation.draft.status.value
            if any(result.get("operation") in draft_operations for result in last_tool_results)
            else None
        )
        return AgentResult(conversation.id, text, draft_status)

    def _execute_tool(
        self, user_id: uuid.UUID, conversation, call, *, commit: bool = True
    ) -> dict[str, Any]:
        if call.name == "update_reminder_draft":
            result = self._update_draft(conversation, call.arguments, commit=commit)
            if result.get("draft_status") == DraftStatus.ready.value:
                return self._create_from_draft(user_id, conversation, commit=commit)
            return result
        if call.name == "create_reminder":
            self._update_draft(conversation, call.arguments, commit=commit)
            return self._create_from_draft(user_id, conversation, commit=commit)
        if call.name == "create_reminders":
            return self._create_many(user_id, conversation, call.arguments, commit=commit)
        if call.name == "list_reminders":
            return self._list(user_id, call.arguments)
        if call.name == "reschedule_reminder":
            return self._reschedule(user_id, call.arguments)
        if call.name == "cancel_reminder":
            return self._cancel(user_id, call.arguments)
        if call.name == "snooze_occurrence":
            return self._snooze(user_id, call.arguments)
        if call.name == "complete_delivered_occurrence":
            return self._complete(user_id, call.arguments)
        if call.name == "create_memory":
            return self._create_memory(user_id, call.arguments)
        if call.name == "retrieve_memories":
            return self._retrieve_memories(user_id, call.arguments)
        return {"operation": "rejected", "error": "unsupported reminder operation"}

    def execute_tool(
        self, user_id: uuid.UUID, conversation, call, *, commit: bool = True
    ) -> dict[str, Any]:
        """Execute one deterministic tool for a channel adapter."""
        return self._execute_tool(user_id, conversation, call, commit=commit)

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

    @staticmethod
    def _completion_response(result: dict[str, Any]) -> str:
        operation = result.get("operation")
        title = result.get("title")
        if operation == "completed":
            return f"Done — I marked {title or 'that reminder'} as completed."
        if operation == "already_completed":
            return f"That reminder{f' ({title})' if title else ''} was already completed."
        if operation == "ambiguous":
            titles = result.get("titles") or []
            suffix = f": {', '.join(titles)}" if titles else ""
            return f"Which reminder should I mark as done{suffix}?"
        if operation == "not_delivered":
            return "I can only mark a reminder done after it has been delivered."
        return "I couldn't find a single delivered reminder to mark done."

    def _complete(self, user_id: uuid.UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            occurrence_id = arguments.get("occurrence_id")
            if occurrence_id:
                result = CompletionService(self.reminders.db).complete_by_id(
                    user_id, uuid.UUID(occurrence_id)
                )
            else:
                result = CompletionService(self.reminders.db).complete_matching(
                    user_id, title=arguments.get("title")
                )
        except (TypeError, ValueError, LookupError) as exc:
            return {"operation": "not_found", "error": str(exc)}
        if result.operation == "ambiguous":
            return {
                "operation": "ambiguous",
                "count": len(result.matches),
                "titles": [item[0].title for item in result.matches],
            }
        title = result.reminder.title if result.reminder else None
        response = {"operation": result.operation, "title": title}
        if result.occurrence is not None:
            response["occurrence_id"] = str(result.occurrence.id)
        return response

    def _update_draft(
        self, conversation, arguments: dict[str, Any], *, commit: bool = True
    ) -> dict[str, Any]:
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
                is_urgent=arguments.get("urgent"),
                commit=commit,
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
                user_id,
                uuid.UUID(reminder_id),
                request,
                notification_identity=self.notification_identity,
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
                notification_identity=self.notification_identity,
            )
        except (KeyError, TypeError, ValueError, ValidationError, LookupError) as exc:
            return {"operation": "not_snoozed", "error": str(exc)}
        user = self.users.get(user_id)
        return {
            "operation": "snoozed",
            "reminder": to_response(reminder, user.timezone).model_dump(mode="json"),
        }

    def _create_from_draft(
        self, user_id: uuid.UUID, conversation, *, commit: bool = True
    ) -> dict[str, Any]:
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
                is_urgent=draft.is_urgent,
                notification_identity=self.notification_identity,
            )
        except ReminderInPastError:
            return {"operation": "not_created", "error": "The reminder time must be strictly in the future."}
        except DuplicateReminderError:
            return {"operation": "not_created", "error": "An equivalent active reminder already exists."}
        except ValueError as exc:
            return {"operation": "not_created", "error": str(exc)}
        self.conversations.mark_created(draft, conversation, commit=commit)
        user = self.users.get(user_id)
        return {
            "operation": "created",
            "reminder": to_response(reminder, user.timezone).model_dump(mode="json"),
        }

    def _create_many(
        self,
        user_id: uuid.UUID,
        conversation,
        arguments: dict[str, Any],
        *,
        commit: bool = True,
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
                    is_urgent=bool(item.get("urgent", False)),
                    notification_identity=self.notification_identity,
                )
                created.append(to_response(reminder, user.timezone).model_dump(mode="json"))
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                rejected.append({"title": item.get("title"), "error": str(exc)})
            except (ReminderInPastError, DuplicateReminderError) as exc:
                rejected.append({"title": item.get("title"), "error": str(exc)})
        if created and conversation.draft:
            self.conversations.mark_created(
                conversation.draft, conversation, commit=commit
            )
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
        database = self.reminders.db.get_bind().url.render_as_string(hide_password=True)
        logger.info(
            "reminder.list_query user_id=%s arguments=%s title=%r start=%s end=%s "
            "occurrence_start=%s occurrence_end=%s status=%s database=%s",
            user_id,
            arguments,
            query.title,
            query.start,
            query.end,
            query.occurrence_start,
            query.occurrence_end,
            query.status,
            database,
        )
        reminders = self.reminders.list_with_occurrences(user_id, query)
        logger.info(
            "reminder.list_result user_id=%s count=%s database=%s",
            user_id,
            len(reminders),
            database,
        )
        formatted = []
        for item, occurrence in reminders:
            reminder_data = to_response(item, user.timezone, occurrence).model_dump(mode="json")
            schedule_display = format_user_datetime(item.schedule, user.timezone)
            # This payload is sent to the language model, so keep the public
            # schedule value conversational instead of exposing an ISO value
            # that can be repeated verbatim in the user's response.
            reminder_data["schedule"] = schedule_display
            reminder_data["schedule_display"] = schedule_display
            if reminder_data.get("occurrence"):
                occurrence_schedule_display = format_user_datetime(
                    occurrence.schedule, user.timezone
                )
                next_due_at_display = format_user_datetime(
                    occurrence.next_due_at, user.timezone
                )
                reminder_data["occurrence"]["schedule"] = occurrence_schedule_display
                reminder_data["occurrence"]["next_due_at"] = next_due_at_display
                reminder_data["occurrence"]["schedule_display"] = occurrence_schedule_display
                reminder_data["occurrence"]["next_due_at_display"] = next_due_at_display
            formatted.append(reminder_data)
        return {
            "operation": "retrieved",
            "count": len(reminders),
            "timezone": user.timezone,
            "reminders": formatted,
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
            "urgent": draft.is_urgent,
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

    @staticmethod
    def _ground_successful_mutation_response(
        text: str, tool_results: list[dict[str, Any]]
    ) -> str:
        """Prevent a final LLM response from contradicting an already-completed mutation."""
        successful_operations = {
            str(result.get("operation"))
            for result in tool_results
            if result.get("operation")
            in {"rescheduled", "created", "created_batch", "canceled", "snoozed"}
        }
        if not successful_operations:
            return text
        lowered = text.casefold()
        contradictory = ("need to", "requires a tool", "once i", "still need", "will be updated")
        if not any(phrase in lowered for phrase in contradictory):
            return text
        if "rescheduled" in successful_operations:
            return "Done — I moved your reminder."
        if "canceled" in successful_operations:
            return "Done — I canceled your reminder."
        if "snoozed" in successful_operations:
            return "Done — I snoozed your reminder."
        return "Done — your reminder was created."

    @staticmethod
    def _redact_internal_ids(value: str) -> str:
        return INTERNAL_ID_PATTERN.sub("[internal id]", value)

    @classmethod
    def _redact_messages(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **message,
                "content": cls._redact_internal_ids(message["content"])
                if isinstance(message.get("content"), str)
                else message.get("content"),
            }
            for message in messages
        ]
