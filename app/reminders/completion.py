from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    OccurrenceStatus,
    Reminder,
    ReminderOccurrence,
    ReminderStatus,
)
from app.notifications.service import NotificationService
from app.urgent_reminders.service import UrgentReminderService
from app.reminders.destinations import default_telegram_identity
from app.reminders.occurrences import advance_recurring_occurrence
from app.reminders.changes import queue_occurrence_changes
from app.users.service import UserService


@dataclass(frozen=True)
class CompletionResult:
    operation: str
    reminder: Reminder | None = None
    occurrence: ReminderOccurrence | None = None
    successor: ReminderOccurrence | None = None
    matches: tuple[tuple[Reminder, ReminderOccurrence], ...] = ()


class CompletionService:
    """Resolve and complete exactly one delivered reminder occurrence."""

    def __init__(self, db: Session):
        self.db = db
        self.users = UserService(db)

    def complete_by_id(
        self,
        user_id: uuid.UUID,
        occurrence_id: uuid.UUID,
        *,
        require_delivered: bool = True,
    ) -> CompletionResult:
        """Complete an owned occurrence without committing the surrounding transaction."""
        occurrence = self.db.scalar(
            select(ReminderOccurrence)
            .join(Reminder)
            .where(
                ReminderOccurrence.id == occurrence_id,
                Reminder.user_id == user_id,
            )
            .with_for_update()
        )
        if occurrence is None:
            return CompletionResult("not_found")
        reminder = self.db.get(Reminder, occurrence.reminder_id, with_for_update=True)
        if reminder is None:
            return CompletionResult("not_found")

        if occurrence.status == OccurrenceStatus.completed:
            return CompletionResult("already_completed", reminder, occurrence)
        if occurrence.status in {OccurrenceStatus.canceled}:
            return CompletionResult("not_delivered", reminder, occurrence)
        if require_delivered and occurrence.status != OccurrenceStatus.notified:
            return CompletionResult("not_delivered", reminder, occurrence)
        if reminder.status != ReminderStatus.active:
            return CompletionResult("not_delivered", reminder, occurrence)

        user = self.users.get(user_id)
        occurrence.status = OccurrenceStatus.completed
        successor = None
        if reminder.is_recurring and reminder.recurrence_frequency:
            successor = advance_recurring_occurrence(reminder, occurrence, user.timezone)
            reminder.schedule = successor.schedule
            identity = default_telegram_identity(self.db, user.id)
            destination = reminder.destination
            if reminder.is_urgent:
                UrgentReminderService(self.db).plan(reminder, successor, telegram_identity=identity)
            else:
                NotificationService(self.db).schedule(
                    reminder,
                    successor,
                    content=f"Reminder: {reminder.title}",
                    identity=identity,
                    channel=None if identity else destination.channel if destination else None,
                    destination_id=reminder.destination_id,
                    destination_snapshot=(
                        identity.external_destination_id
                        if identity is not None
                        else destination.identity if destination else None
                    ),
                )
        else:
            reminder.status = ReminderStatus.completed

        self.db.flush()
        queue_occurrence_changes(
            self.db,
            [occurrence.id] + ([successor.id] if successor is not None else []),
        )

        return CompletionResult("completed", reminder, occurrence, successor)

    def complete_matching(
        self,
        user_id: uuid.UUID,
        *,
        title: str | None = None,
    ) -> CompletionResult:
        """Resolve one delivered occurrence; ambiguity is always non-mutating."""
        statement = (
            select(Reminder, ReminderOccurrence)
            .join(ReminderOccurrence, ReminderOccurrence.reminder_id == Reminder.id)
            .where(
                Reminder.user_id == user_id,
                Reminder.status == ReminderStatus.active,
                ReminderOccurrence.status == OccurrenceStatus.notified,
            )
            .order_by(ReminderOccurrence.schedule.asc())
            .with_for_update()
        )
        if title and title.strip():
            statement = statement.where(Reminder.title.ilike(f"%{title.strip()}%"))
        matches = tuple(self.db.execute(statement).all())
        if len(matches) == 1:
            return self.complete_by_id(user_id, matches[0][1].id)
        if len(matches) > 1:
            return CompletionResult("ambiguous", matches=matches)

        # If there is no currently delivered occurrence, an exact title can
        # still identify a single completed occurrence for an idempotent reply.
        completed_statement = (
            select(Reminder, ReminderOccurrence)
            .join(ReminderOccurrence, ReminderOccurrence.reminder_id == Reminder.id)
            .where(
                Reminder.user_id == user_id,
                ReminderOccurrence.status == OccurrenceStatus.completed,
            )
            .order_by(ReminderOccurrence.updated_at.desc())
        )
        if title and title.strip():
            completed_statement = completed_statement.where(
                Reminder.title.ilike(f"%{title.strip()}%")
            )
        completed_matches = tuple(self.db.execute(completed_statement).all())
        if len(completed_matches) == 1:
            reminder, occurrence = completed_matches[0]
            return CompletionResult("already_completed", reminder, occurrence)
        if len(completed_matches) > 1:
            return CompletionResult("ambiguous", matches=completed_matches)
        return CompletionResult("not_found")
