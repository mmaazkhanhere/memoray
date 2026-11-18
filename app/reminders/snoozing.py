import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ChannelIdentity,
    ChannelIdentityStatus,
    ChannelKind,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
)
from app.notifications.service import NotificationService
from app.reminders.destinations import default_telegram_identity
from app.reminders.errors import OccurrenceNotNotifiedError, ReminderNotActiveError
from app.reminders.changes import queue_occurrence_change
from app.schemas import SnoozeRequest
from app.users.service import UserService


class ReminderSnoozer:
    def __init__(self, db: Session):
        self.db = db
        self.users = UserService(db)

    def snooze(
        self,
        user_id: uuid.UUID,
        reminder_id: uuid.UUID,
        occurrence_id: uuid.UUID,
        request: SnoozeRequest,
        *,
        notification_identity: ChannelIdentity | None = None,
    ) -> Reminder:
        self.users.get(user_id)
        reminder = self.db.scalar(
            select(Reminder)
            .options(selectinload(Reminder.occurrences))
            .where(Reminder.id == reminder_id, Reminder.user_id == user_id)
        )
        if reminder is None:
            raise LookupError("reminder not found")
        if reminder.status != ReminderStatus.active:
            raise ReminderNotActiveError("the parent reminder is not active")
        occurrence = next((item for item in reminder.occurrences if item.id == occurrence_id), None)
        if occurrence is None:
            raise LookupError("occurrence not found")
        if occurrence.status != OccurrenceStatus.notified:
            raise OccurrenceNotNotifiedError("only notified occurrences can be snoozed")
        occurrence.next_due_at = datetime.now(timezone.utc) + timedelta(seconds=request.duration_seconds)
        occurrence.status = OccurrenceStatus.pending
        identity = (
            notification_identity
            if notification_identity is not None
            and notification_identity.channel == ChannelKind.telegram
            else None
        )
        identity = identity or default_telegram_identity(self.db, user_id)
        if identity is not None and (
            identity.user_id != user_id or identity.status != ChannelIdentityStatus.linked
        ):
            raise ValueError("notification identity is not linked")
        NotificationService(self.db).replace_pending(
            reminder,
            occurrence,
            content=f"Reminder: {reminder.title}",
            identity=identity,
            channel=None if identity else reminder.destination.channel if reminder.destination else None,
            destination_id=reminder.destination_id,
            destination_snapshot=(
                identity.external_destination_id
                if identity is not None
                else reminder.destination.identity if reminder.destination else None
            ),
        )
        queue_occurrence_change(self.db, occurrence.id)
        self.db.commit()
        self.db.refresh(reminder)
        return reminder
