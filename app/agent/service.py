import uuid
from datetime import datetime, timezone
import json
from typing import Any

from pydantic import ValidationError

from app.agent.tools import TOOLS
from app.llm.protocol import LLMProvider
from app.reminders.serialization import to_response
from app.reminders.errors import DuplicateReminderError, ReminderInPastError
from app.reminders.service import ReminderService
from app.schemas import ReminderCreate, ReminderQuery
from app.time_utils import to_user_timezone
from app.users.service import UserService

SYSTEM_PROMPT = """You are Memoray, a reminder assistant. You can only create and retrieve reminders.
Use create_reminder when the user wants a reminder created; it requires title and exact date/time.
Use list_reminders to retrieve reminders; omit filters to retrieve all. Resolve relative dates in the
user's timezone. If required information is missing, ask for it. For unrelated requests, say that
you can only create or retrieve reminders and ask which they would like to do. Never invent fields.
After a tool returns, write a warm, concise, personalized response based only on the tool result.
For a successful creation, confirm the title and localized schedule. For retrieval, summarize the
matching reminders naturally. If a tool reports an error, explain it clearly and ask for the next
needed input. Do not claim an operation succeeded unless its tool result says it did. Your response
must contain no more than 25 words.
"""

class ReminderAgent:
    def __init__(self, provider: LLMProvider, reminder_service: ReminderService, user_service: UserService):
        self.provider = provider
        self.reminders = reminder_service
        self.users = user_service

    def handle(self, user_id: uuid.UUID, message: str) -> str:
        user = self.users.get(user_id)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"User timezone: {user.timezone}\n"
                    f"Current time: {to_user_timezone(datetime.now(timezone.utc), user.timezone).isoformat()}\n"
                    f"{message}"
                ),
            },
        ]
        response = self.provider.complete(messages, TOOLS)
        if not response.tool_calls:
            return response.text or "I can help create or retrieve reminders."
        tool_messages = []
        assistant_tool_calls = []
        for index, call in enumerate(response.tool_calls):
            call_id = call.id or f"call_{index}"
            assistant_tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
            )
            if call.name == "create_reminder":
                result = self._create(user_id, call.arguments)
            elif call.name == "list_reminders":
                result = self._list(user_id, call.arguments)
            else:
                result = {"operation": "rejected", "error": "unsupported reminder operation"}
            tool_messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)}
            )

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": response.text or "",
            "tool_calls": assistant_tool_calls,
        }
        final_response = self.provider.complete(
            messages + [assistant_message, *tool_messages],
            [],
        )
        if not final_response.text:
            raise RuntimeError("LLM did not return a final response after the reminder tool call")
        return self._limit_words(final_response.text)

    def _create(self, user_id: uuid.UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            data = ReminderCreate(
                title=arguments.get("title", ""),
                schedule=datetime.fromisoformat(arguments["scheduled_at"]),
            )
        except (KeyError, ValueError, TypeError, ValidationError):
            return {
                "operation": "not_created",
                "error": "I need both a reminder title and an exact date and time.",
            }
        try:
            reminder = self.reminders.create(user_id, data)
        except ReminderInPastError:
            return {
                "operation": "not_created",
                "error": "The reminder time must be strictly in the future.",
            }
        except DuplicateReminderError:
            return {
                "operation": "not_created",
                "error": "An active reminder with the same task and time already exists.",
            }
        user = self.users.get(user_id)
        return {
            "operation": "created",
            "reminder": to_response(reminder, user.timezone).model_dump(mode="json"),
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
        if not reminders:
            return {"operation": "retrieved", "count": 0, "reminders": []}
        return {
            "operation": "retrieved",
            "count": len(reminders),
            "reminders": [
                to_response(item, user.timezone).model_dump(mode="json") for item in reminders
            ],
        }

    @staticmethod
    def _parse_optional(value: str | None):
        return datetime.fromisoformat(value) if value else None

    @staticmethod
    def _limit_words(text: str, limit: int = 25) -> str:
        words = text.split()
        return " ".join(words[:limit])

