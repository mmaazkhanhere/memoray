import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OccurrenceStatus, Reminder, ReminderOccurrence, ReminderStatus
from app.reminders.destinations import DestinationService


class DeliveryService:
    """Small state-transition boundary for notification workers."""

    def __init__(self, db: Session):
        self.db = db
        self.destinations = DestinationService(db)

    def due_occurrences(self, limit: int = 100) -> list[ReminderOccurrence]:
        now = datetime.now(timezone.utc)
        statement = (
            select(ReminderOccurrence)
            .join(Reminder)
            .where(
                Reminder.status == ReminderStatus.active,
                ReminderOccurrence.status == OccurrenceStatus.pending,
                ReminderOccurrence.next_due_at <= now,
            )
            .order_by(ReminderOccurrence.next_due_at.asc())
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())

    def mark_notified(self, occurrence_id: uuid.UUID) -> ReminderOccurrence:
        occurrence = self.db.scalar(
            select(ReminderOccurrence).where(ReminderOccurrence.id == occurrence_id)
        )
        if occurrence is None:
            raise LookupError("occurrence not found")
        reminder = self.db.get(Reminder, occurrence.reminder_id)
        if reminder is None or reminder.status != ReminderStatus.active:
            raise ValueError("parent reminder is not active")
        if occurrence.status != OccurrenceStatus.pending:
            raise ValueError("occurrence is not pending")
        if reminder.destination_id is None:
            raise ValueError("reminder has no delivery destination")
        self.destinations.get_owned_valid(reminder.user_id, reminder.destination_id)
        occurrence.status = OccurrenceStatus.notified
        self.db.commit()
        self.db.refresh(occurrence)
        return occurrence
