"""Channel-neutral reminder use cases.

Adapters (REST, the conversational agent, Vapi, and MCP) should depend on this
module rather than reaching into each other's orchestration code.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChannelIdentity, ChannelIdentityStatus, ChannelKind
from app.reminders.canceling import ReminderCanceler
from app.reminders.completion import CompletionService
from app.reminders.rescheduling import ReminderRescheduler
from app.reminders.serialization import to_response
from app.reminders.service import ReminderService
from app.reminders.snoozing import ReminderSnoozer
from app.schemas import (
    CancellationScope,
    ReminderCancelRequest,
    ReminderQuery,
    ReminderRescheduleRequest,
    RescheduleScope,
    SnoozeRequest,
)
from app.users.service import UserService


class ReminderApplication:
    """Owns reminder commands exposed to non-conversational adapters."""

    def __init__(
        self,
        db: Session,
        *,
        notification_identity: ChannelIdentity | None = None,
        require_telegram: bool = False,
    ):
        self.db = db
        self.reminders = ReminderService(db)
        self.users = UserService(db)
        self.notification_identity = notification_identity
        self.require_telegram = require_telegram

    def _telegram_identity(self, user_id: uuid.UUID) -> ChannelIdentity:
        identity = self.db.scalar(
            select(ChannelIdentity)
            .where(
                ChannelIdentity.user_id == user_id,
                ChannelIdentity.channel == ChannelKind.telegram,
                ChannelIdentity.status == ChannelIdentityStatus.linked,
            )
            .order_by(ChannelIdentity.is_default.desc(), ChannelIdentity.created_at.asc())
        )
        if identity is None:
            raise ValueError("link a Telegram account before creating reminders")
        return identity

    def _delivery_identity(self, user_id: uuid.UUID) -> ChannelIdentity | None:
        if self.notification_identity is not None:
            return self.notification_identity
        if self.require_telegram:
            return self._telegram_identity(user_id)
        return None

    def create(
        self,
        user_id: uuid.UUID,
        *,
        title: str,
        schedule_date: date,
        schedule_time: time,
        recurrence_frequency=None,
        recurrence_interval: int = 1,
    ) -> dict:
        reminder = self.reminders.create_from_local_parts(
            user_id,
            title=title,
            schedule_date=schedule_date,
            schedule_time=schedule_time,
            recurrence_frequency=recurrence_frequency,
            recurrence_interval=recurrence_interval,
            notification_identity=self._delivery_identity(user_id),
        )
        user = self.users.get(user_id)
        return to_response(reminder, user.timezone).model_dump(mode="json")

    def list(self, user_id: uuid.UUID, query: ReminderQuery) -> list[dict]:
        user = self.users.get(user_id)
        return [
            to_response(item, user.timezone, occurrence).model_dump(mode="json")
            for item, occurrence in self.reminders.list_with_occurrences(user_id, query)
        ]

    def reschedule(
        self,
        user_id: uuid.UUID,
        reminder_id: uuid.UUID,
        *,
        schedule: datetime,
        scope: RescheduleScope,
        occurrence_id: uuid.UUID | None = None,
    ) -> dict:
        request = ReminderRescheduleRequest(
            schedule=schedule, scope=scope, occurrence_id=occurrence_id
        )
        reminder = ReminderRescheduler(self.db).reschedule(
            user_id,
            reminder_id,
            request,
            notification_identity=self._delivery_identity(user_id),
        )
        user = self.users.get(user_id)
        return to_response(reminder, user.timezone).model_dump(mode="json")

    def cancel(
        self,
        user_id: uuid.UUID,
        reminder_id: uuid.UUID,
        *,
        scope: CancellationScope,
        occurrence_id: uuid.UUID | None = None,
    ) -> dict:
        reminder = ReminderCanceler(self.db).cancel(
            user_id,
            reminder_id,
            ReminderCancelRequest(scope=scope, occurrence_id=occurrence_id),
        )
        user = self.users.get(user_id)
        return to_response(reminder, user.timezone).model_dump(mode="json")

    def snooze(
        self,
        user_id: uuid.UUID,
        reminder_id: uuid.UUID,
        occurrence_id: uuid.UUID,
        duration_seconds: int,
    ) -> dict:
        reminder = ReminderSnoozer(self.db).snooze(
            user_id,
            reminder_id,
            occurrence_id,
            SnoozeRequest(duration_seconds=duration_seconds),
            notification_identity=self._delivery_identity(user_id),
        )
        user = self.users.get(user_id)
        return to_response(reminder, user.timezone).model_dump(mode="json")

    def complete(
        self, user_id: uuid.UUID, *, occurrence_id: uuid.UUID | None = None, title: str | None = None
    ) -> dict:
        if occurrence_id:
            result = CompletionService(self.db).complete_by_id(user_id, occurrence_id)
        else:
            result = CompletionService(self.db).complete_matching(user_id, title=title)
        if result.operation == "ambiguous":
            return {
                "operation": "ambiguous",
                "count": len(result.matches),
                "titles": [item[0].title for item in result.matches],
            }
        return {
            "operation": result.operation,
            "title": result.reminder.title if result.reminder else None,
            "occurrence_id": str(result.occurrence.id) if result.occurrence else None,
        }
