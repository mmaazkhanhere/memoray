from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.factory import create_tool_agent
from app.conversations.service import ConversationService
from app.models import (
    ChannelIdentity,
    ChannelIdentityStatus,
    ChannelKind,
    PhoneLink,
    User,
    UserStatus,
    VapiCall,
    VapiCallStatus,
    VapiToolExecution,
)
from app.phones.service import PhoneService

logger = logging.getLogger(__name__)

class VapiAuthenticationError(ValueError):
    pass


class VapiCallService:
    """Authenticates callers and executes Vapi tools against shared services."""

    def __init__(self, db: Session):
        self.db = db

    def authenticate_caller(self, external_call_id: str, raw_number: str) -> VapiCall:
        if not external_call_id:
            raise VapiAuthenticationError("This call could not be identified.")
        try:
            caller = PhoneService.normalize_e164(raw_number)
        except ValueError as exc:
            raise VapiAuthenticationError(
                "This phone number is not registered for Memoray."
            ) from exc

        identity = self.db.scalar(
            select(ChannelIdentity)
            .join(User, User.id == ChannelIdentity.user_id)
            .join(PhoneLink, PhoneLink.user_id == ChannelIdentity.user_id)
            .where(
                ChannelIdentity.channel == ChannelKind.phone,
                ChannelIdentity.external_principal_id == caller,
                ChannelIdentity.status == ChannelIdentityStatus.linked,
                PhoneLink.e164_number == caller,
                PhoneLink.is_active.is_(True),
                User.status == UserStatus.active,
            )
        )
        if identity is None:
            raise VapiAuthenticationError("This phone number is not registered for Memoray.")

        existing = self.db.scalar(
            select(VapiCall).where(VapiCall.external_call_id == external_call_id)
        )
        if existing is not None:
            if existing.caller_e164 != caller or existing.user_id != identity.user_id:
                raise VapiAuthenticationError("This call could not be authenticated.")
            if existing.status == VapiCallStatus.ended:
                raise VapiAuthenticationError("This call is no longer active.")
            return existing

        conversation = ConversationService(self.db).get_or_create(identity.user_id, None)
        call = VapiCall(
            external_call_id=external_call_id,
            user_id=identity.user_id,
            caller_e164=caller,
            conversation_id=conversation.id,
            status=VapiCallStatus.active,
        )
        try:
            with self.db.begin_nested():
                self.db.add(call)
                self.db.flush()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.scalar(
                select(VapiCall).where(VapiCall.external_call_id == external_call_id)
            )
            if existing is None or existing.caller_e164 != caller:
                raise VapiAuthenticationError("This call could not be authenticated.")
            return existing
        self.db.commit()
        self.db.refresh(call)
        return call

    def end_call(self, external_call_id: str) -> None:
        call = self.db.scalar(select(VapiCall).where(VapiCall.external_call_id == external_call_id))
        if call is None:
            return
        call.status = VapiCallStatus.ended
        call.ended_at = datetime.now(timezone.utc)
        self.db.commit()

    def authenticated_call(self, external_call_id: str, raw_number: str | None) -> VapiCall:
        call = self.db.scalar(select(VapiCall).where(VapiCall.external_call_id == external_call_id))
        if call is None or call.status != VapiCallStatus.active:
            raise VapiAuthenticationError("This call is not active.")
        if not raw_number:
            return call
        try:
            caller = PhoneService.normalize_e164(raw_number)
        except ValueError as exc:
            raise VapiAuthenticationError("This call could not be authenticated.") from exc
        if caller != call.caller_e164:
            raise VapiAuthenticationError("This call could not be authenticated.")
        return call

    def execute_tool(
        self,
        call: VapiCall,
        *,
        tool_call_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if call.status != VapiCallStatus.active:
            return {"operation": "rejected", "error": "This call is no longer active."}
        arguments_hash = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        existing = self.db.scalar(
            select(VapiToolExecution).where(
                VapiToolExecution.vapi_call_id == call.id,
                VapiToolExecution.external_tool_call_id == tool_call_id,
            )
        )
        if existing is not None:
            if existing.arguments_hash != arguments_hash or existing.tool_name != name:
                return {"operation": "rejected", "error": "This tool call ID was already used."}
            if existing.result.get("operation") == "processing":
                return {
                    "operation": "failed",
                    "error": "The previous request is still completing; please retry once.",
                }
            return existing.result

        try:
            with self.db.begin_nested():
                execution = VapiToolExecution(
                    vapi_call_id=call.id,
                    external_tool_call_id=tool_call_id,
                    tool_name=name,
                    arguments_hash=arguments_hash,
                    result={"operation": "processing"},
                )
                self.db.add(execution)
                self.db.flush()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.scalar(
                select(VapiToolExecution).where(
                    VapiToolExecution.vapi_call_id == call.id,
                    VapiToolExecution.external_tool_call_id == tool_call_id,
                )
            )
            if existing is not None and existing.arguments_hash == arguments_hash:
                if existing.result.get("operation") == "processing":
                    return {
                        "operation": "failed",
                        "error": "The previous request is still completing; please retry once.",
                    }
                return existing.result
            return {"operation": "rejected", "error": "This tool call is already being processed."}

        if name in {"reschedule_reminder", "cancel_reminder"}:
            guarded = self._guard_current_target(call, arguments)
            if guarded is not None:
                result = guarded
            else:
                result = self._run_shared_tool(call, name, arguments)
        else:
            result = self._run_shared_tool(call, name, arguments)

        self._add_spoken_confirmation(result)
        if execution not in self.db:
            self.db.add(execution)
        execution.result = result
        execution.completed_at = datetime.now(timezone.utc)
        self._update_call_context(call, result)
        self.db.commit()
        return result

    @staticmethod
    def _add_spoken_confirmation(result: dict[str, Any]) -> None:
        if result.get("operation") == "created_batch" and result.get("created_count"):
            created = result.get("created") or []
            urgent_count = sum(
                1 for item in created if isinstance(item, dict) and item.get("is_urgent")
            )
            if urgent_count == len(created):
                delivery_text = "I will call you for each urgent reminder, then use Telegram if you do not answer."
            elif urgent_count:
                delivery_text = "I will call you for the urgent reminders and use Telegram for the others."
            else:
                delivery_text = "I will notify you on Telegram for each one."
            result["speak"] = f"I created {result['created_count']} reminders. {delivery_text}"
            return
        if result.get("operation") not in {"created", "rescheduled", "snoozed", "canceled"}:
            return
        reminder = result.get("reminder")
        if not isinstance(reminder, dict):
            return
        title = str(reminder.get("title") or "that reminder")
        schedule = reminder.get("schedule")
        if schedule:
            try:
                value = datetime.fromisoformat(str(schedule))
                spoken_time = (
                    f"{value.strftime('%A, %B')} {value.day} at "
                    f"{value.hour % 12 or 12}:{value.minute:02d} {value.strftime('%p')}"
                )
            except (ValueError, TypeError):
                spoken_time = str(schedule)
        else:
            spoken_time = "the revised time"
        if result.get("operation") == "created":
            delivery_text = (
                "I will call you, then use Telegram if you do not answer twice."
                if reminder.get("is_urgent")
                else "I will notify you on Telegram."
            )
            result["speak"] = f"Your reminder, {title}, is set for {spoken_time}. {delivery_text}"
        elif result.get("operation") == "rescheduled":
            result["speak"] = f"Your reminder, {title}, is now set for {spoken_time}."
        elif result.get("operation") == "snoozed":
            result["speak"] = f"I snoozed {title}."
        else:
            result["speak"] = f"I canceled {title}."

    def _run_shared_tool(
        self, call: VapiCall, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        identity = self._telegram_identity(call.user_id)
        reminder_tools = {
            "create_reminder",
            "create_reminders",
            "update_reminder_draft",
            "reschedule_reminder",
            "snooze_occurrence",
        }
        if name in reminder_tools and identity is None:
            operation = {
                "reschedule_reminder": "not_rescheduled",
                "snooze_occurrence": "not_snoozed",
            }.get(name, "not_created")
            return {
                "operation": operation,
                "error": "A linked Telegram account is required for call reminders.",
            }
        try:
            agent = create_tool_agent(self.db, notification_identity=identity)
            from app.llm.protocol import ToolCall

            return agent.execute_tool(
                call.user_id,
                self._conversation(call),
                ToolCall(id="vapi", name=name, arguments=arguments),
            )
        except Exception:
            logger.exception("Vapi shared tool execution failed", extra={"tool_name": name})
            self.db.rollback()
            return {"operation": "failed", "error": "The operation could not be completed."}

    def _conversation(self, call: VapiCall):
        from app.models import ReminderConversation

        return self.db.get(ReminderConversation, call.conversation_id)

    def _telegram_identity(self, user_id: uuid.UUID) -> ChannelIdentity | None:
        return self.db.scalar(
            select(ChannelIdentity)
            .where(
                ChannelIdentity.user_id == user_id,
                ChannelIdentity.channel == ChannelKind.telegram,
                ChannelIdentity.status == ChannelIdentityStatus.linked,
            )
            .order_by(ChannelIdentity.is_default.desc(), ChannelIdentity.created_at.asc())
        )

    @staticmethod
    def _reminder_id(result: dict[str, Any]) -> uuid.UUID | None:
        reminder = result.get("reminder")
        if not isinstance(reminder, dict) or not reminder.get("id"):
            return None
        try:
            return uuid.UUID(str(reminder["id"]))
        except ValueError:
            return None

    def _guard_current_target(
        self, call: VapiCall, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        supplied = arguments.get("reminder_id")
        if not supplied:
            return {
                "operation": "discovery_required",
                "error": "First list reminders so I can identify the exact reminder.",
            }
        if call.last_reminder_id is None:
            return {
                "operation": "discovery_required",
                "error": (
                    "First list reminders using the caller's title or a distinctive title "
                    "fragment so I can identify the exact reminder."
                ),
            }
        try:
            supplied_id = uuid.UUID(str(supplied))
        except ValueError:
            return {
                "operation": "target_not_discovered",
                "error": "That reminder was not identified.",
            }
        if supplied_id != call.last_reminder_id:
            return {
                "operation": "target_ambiguous",
                "error": "Please clarify which reminder you mean.",
            }
        return None

    def _update_call_context(self, call: VapiCall, result: dict[str, Any]) -> None:
        operation = result.get("operation")
        if operation == "retrieved":
            reminders = result.get("reminders") or []
            if result.get("count") == 1 and reminders:
                call.last_reminder_id = uuid.UUID(reminders[0]["id"])
                call.target_ambiguous = False
                occurrence = reminders[0].get("occurrence") or {}
                call.last_occurrence_id = (
                    uuid.UUID(occurrence["id"]) if occurrence.get("id") else None
                )
            else:
                call.last_reminder_id = None
                call.last_occurrence_id = None
                call.target_ambiguous = bool(result.get("count"))
            return
        if operation == "created":
            call.last_reminder_id = self._reminder_id(result)
            call.target_ambiguous = False
            reminder = result.get("reminder") or {}
            occurrence = reminder.get("occurrence") or {}
            call.last_occurrence_id = uuid.UUID(occurrence["id"]) if occurrence.get("id") else None
            return
        if operation == "created_batch":
            created = result.get("created") or []
            if result.get("created_count") == 1:
                call.last_reminder_id = uuid.UUID(created[0]["id"])
                occurrence = created[0].get("occurrence") or {}
                call.last_occurrence_id = (
                    uuid.UUID(occurrence["id"]) if occurrence.get("id") else None
                )
            else:
                call.last_reminder_id = None
                call.last_occurrence_id = None
                call.target_ambiguous = bool(result.get("created_count"))
            return
        if operation in {"rescheduled", "canceled", "snoozed"}:
            if operation == "canceled":
                call.last_reminder_id = None
                call.last_occurrence_id = None
                call.target_ambiguous = False
                return
            call.last_reminder_id = self._reminder_id(result)
            call.target_ambiguous = False
            reminder = result.get("reminder") or {}
            occurrence = reminder.get("occurrence") or {}
            call.last_occurrence_id = uuid.UUID(occurrence["id"]) if occurrence.get("id") else None
