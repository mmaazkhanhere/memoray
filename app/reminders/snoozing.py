import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import ChannelIdentity, ChannelIdentityStatus, OccurrenceStatus, Reminder, ReminderStatus
from app.notifications.service import NotificationService
from app.reminders.errors import OccurrenceNotNotifiedError, ReminderNotActiveError
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
        identity = self.db.scalar(
            select(ChannelIdentity)
            .where(
                ChannelIdentity.user_id == user_id,
                ChannelIdentity.status == ChannelIdentityStatus.linked,
                ChannelIdentity.is_default.is_(True),
            )
            .order_by(ChannelIdentity.created_at.asc())
        )
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
        self.db.commit()
        self.db.refresh(reminder)
        return reminder
