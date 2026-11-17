import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import OccurrenceStatus, Reminder, ReminderStatus
from app.notifications.service import NotificationService
from app.reminders.errors import (
    OccurrenceNotCancelableError,
    ReminderNotActiveError,
)
from app.reminders.changes import queue_occurrence_changes
from app.reminders.occurrences import advance_recurring_occurrence
from app.schemas import CancellationScope, ReminderCancelRequest
from app.users.service import UserService


class ReminderCanceler:
    def __init__(self, db: Session):
        self.db = db
        self.users = UserService(db)

    def cancel(
        self,
        user_id: uuid.UUID,
        reminder_id: uuid.UUID,
        request: ReminderCancelRequest,
    ) -> Reminder:
        user = self.users.get(user_id)
        reminder = self.db.scalar(
            select(Reminder)
            .options(selectinload(Reminder.occurrences))
            .where(Reminder.id == reminder_id, Reminder.user_id == user_id)
        )
        if reminder is None:
            raise LookupError("reminder not found")
        if reminder.status != ReminderStatus.active:
            raise ReminderNotActiveError("only active reminders can be canceled")

        if request.scope == CancellationScope.one_time:
            if reminder.is_recurring:
                raise ValueError("one_time scope requires a non-recurring reminder")
            if request.occurrence_id is not None:
                raise ValueError("one_time scope does not accept an occurrence ID")
            self._cancel_pending_occurrences(reminder)
            reminder.status = ReminderStatus.canceled
        elif request.scope == CancellationScope.occurrence:
            if not reminder.is_recurring:
                raise ValueError("occurrence scope requires a recurring reminder")
            if request.occurrence_id is None:
                raise ValueError("occurrence scope requires an occurrence ID")
            occurrence = next(
                (item for item in reminder.occurrences if item.id == request.occurrence_id), None
            )
            if occurrence is None:
                raise LookupError("occurrence not found")
            if occurrence.status != OccurrenceStatus.pending:
                raise OccurrenceNotCancelableError("only pending occurrences can be canceled")
            occurrence.status = OccurrenceStatus.canceled
            NotificationService(self.db).cancel_for_occurrence(occurrence.id)
            advance_recurring_occurrence(reminder, occurrence, user.timezone)
            self._update_next_schedule(reminder)
        elif request.scope == CancellationScope.series:
            if not reminder.is_recurring:
                raise ValueError("series scope requires a recurring reminder")
            if request.occurrence_id is not None:
                raise ValueError("series scope does not accept an occurrence ID")
            reminder.status = ReminderStatus.canceled
            self._cancel_pending_occurrences(reminder)
        else:
            raise ValueError("unsupported cancellation scope")

        self.db.flush()
        queue_occurrence_changes(
            self.db,
            [item.id for item in reminder.occurrences if item.status in {
                OccurrenceStatus.canceled,
                OccurrenceStatus.pending,
            }],
        )
        self.db.commit()
        self.db.refresh(reminder)
        return reminder

    def _cancel_pending_occurrences(self, reminder: Reminder) -> None:
        for occurrence in reminder.occurrences:
            if occurrence.status == OccurrenceStatus.pending:
                occurrence.status = OccurrenceStatus.canceled
                NotificationService(self.db).cancel_for_occurrence(occurrence.id)

    @staticmethod
    def _update_next_schedule(reminder: Reminder) -> None:
        pending = [
            item for item in reminder.occurrences if item.status == OccurrenceStatus.pending
        ]
        if pending:
            reminder.schedule = min(pending, key=lambda item: item.schedule).schedule
