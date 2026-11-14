import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

from pydantic import ValidationError

from app.agent.tools import TOOLS
from app.conversations.service import ConversationService
from app.llm.protocol import LLMProvider
from app.models import ConversationMessageRole, DraftStatus, RecurrenceFrequency
from app.reminders.errors import DuplicateReminderError, ReminderInPastError
from app.reminders.serialization import to_response
from app.reminders.service import ReminderService
from app.schemas import ReminderCreate, ReminderQuery
from app.time_utils import to_user_timezone
from app.users.service import UserService

SYSTEM_PROMPT = """You are Memoray, a reminder assistant. You can only create and retrieve reminders.
Use update_reminder_draft to save any details from the current message when creation details are
incomplete. Required fields are title, date, and time. If the user requests a recurring reminder,
recurring=true and recurrence_frequency (daily, weekly, monthly, or yearly) are also required.
For phrases such as "every 3 days", use recurrence_frequency=daily and recurrence_interval=3.
Use create_reminder only when all required fields are available. If the user asks for multiple
distinct reminders in one message, use create_reminders and include every complete item. Use list_reminders for retrieval;
questions such as 'do I have anything today?' mean today's date in the user's timezone. Resolve
relative dates in the user's timezone using the supplied current time. Never invent missing values.
For unrelated requests, explain that you can only create or retrieve reminders. After a tool returns,
write a warm, concise, personalized response based only on its result. Do not claim success unless
the result says it succeeded. Keep the response at 25 words or fewer.
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
    ):
        self.provider = provider
        self.reminders = reminder_service
        self.users = user_service
        self.conversations = conversation_service

    def handle(
        self,
        user_id: uuid.UUID,
        message: str,
        conversation_id: uuid.UUID | None = None,
    ) -> AgentResult:
        user = self.users.get(user_id)
        conversation = self.conversations.get_or_create(user_id, conversation_id)
        self.conversations.record_message(conversation.id, ConversationMessageRole.user, message)

        history = self.conversations.recent_messages(conversation)
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
        response = self.provider.complete(messages, TOOLS)
        if not response.tool_calls:
            text = self._limit_words(response.text or "I can help create or retrieve reminders.")
            self.conversations.record_message(conversation.id, ConversationMessageRole.assistant, text)
            return AgentResult(conversation.id, text, draft.status.value if draft else None)

        tool_results = []
        for call in response.tool_calls:
            if call.name == "update_reminder_draft":
                result = self._update_draft(conversation, call.arguments)
                if result.get("draft_status") == DraftStatus.ready.value:
                    result = self._create_from_draft(user_id, conversation)
            elif call.name == "create_reminder":
                self._update_draft(conversation, call.arguments)
                result = self._create_from_draft(user_id, conversation)
            elif call.name == "create_reminders":
                result = self._create_many(user_id, conversation, call.arguments)
            elif call.name == "list_reminders":
                result = self._list(user_id, call.arguments)
            else:
                result = {"operation": "rejected", "error": "unsupported reminder operation"}
            tool_results.append(result)

        final_context = {
            "role": "user",
            "content": (
                "Reminder tool results are below. Do not call any tools. "
                "Write only the final conversational response, based on these results:\n"
                f"{json.dumps(tool_results)}"
            ),
        }
        final_response = self.provider.complete(messages + [final_context], [])
        if not final_response.text:
            raise RuntimeError("LLM did not return a final response after the reminder tool call")
        text = self._limit_words(final_response.text)
        self.conversations.record_message(conversation.id, ConversationMessageRole.assistant, text)
        return AgentResult(conversation.id, text, conversation.draft.status.value)

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
                end=self._parse_optional(arguments.get("end")),
                status=arguments.get("status"),
            )
        except (ValueError, ValidationError):
            return {"operation": "not_retrieved", "error": "The reminder filter was invalid."}
        user = self.users.get(user_id)
        reminders = self.reminders.list(user_id, query)
        return {
            "operation": "retrieved",
            "count": len(reminders),
            "reminders": [to_response(item, user.timezone).model_dump(mode="json") for item in reminders],
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
    def _parse_optional(value: str | None):
        return datetime.fromisoformat(value) if value else None

    @staticmethod
    def _limit_words(text: str, limit: int = 25) -> str:
        return " ".join(text.split()[:limit])
