from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import (
    OccurrenceStatus,
    RecurrenceFrequency,
    Reminder,
    ReminderOccurrence,
    ReminderStatus,
)
from app.reminders.errors import DuplicateReminderError, ReminderInPastError
from app.reminders.occurrences import advance_recurring_occurrence
from app.reminders.destinations import DestinationService
from app.recurrence.schedule import next_occurrence
from app.schemas import ReminderCreate, ReminderListStatus, ReminderQuery
from app.time_utils import to_utc, to_user_timezone
from app.users.service import UserService


class ReminderService:
    def __init__(self, db: Session):
        self.db = db
        self.users = UserService(db)
        self.destinations = DestinationService(db)

    def create(self, user_id: uuid.UUID, data: ReminderCreate) -> Reminder:
        user = self.users.get(user_id)
        if data.is_recurring and data.recurrence_frequency is None:
            raise ValueError("recurrence frequency is required for recurring reminders")
        if data.recurrence_interval < 1:
            raise ValueError("recurrence interval must be at least 1")
        is_recurring = data.is_recurring or data.recurrence_frequency is not None
        schedule = to_utc(data.schedule, user.timezone)
        if schedule <= datetime.now(timezone.utc):
            raise ReminderInPastError("reminder schedule must be in the future")
        normalized_title = data.title.strip().casefold()
        destination = (
            self.destinations.get_owned_valid(user.id, data.destination_id)
            if data.destination_id is not None
            else self.destinations.default_for_user(user.id)
        )
        duplicate = self.db.scalar(
            select(Reminder)
            .where(
                Reminder.user_id == user.id,
                Reminder.normalized_title == normalized_title,
                Reminder.schedule == schedule,
                Reminder.status == ReminderStatus.active,
                Reminder.occurrences.any(ReminderOccurrence.status == OccurrenceStatus.pending),
            )
            .limit(1)
        )
        if duplicate is not None:
            raise DuplicateReminderError("an active reminder with the same title and schedule already exists")
        reminder = Reminder(
            user_id=user.id,
            title=data.title.strip(),
            normalized_title=normalized_title,
            schedule=schedule,
            is_recurring=is_recurring,
            recurrence_rule=data.recurrence_rule,
            recurrence_frequency=data.recurrence_frequency,
            recurrence_interval=data.recurrence_interval,
            destination_id=destination.id,
            series_anchor_schedule=schedule,
            status=ReminderStatus.active,
        )
        reminder.occurrences.append(
            ReminderOccurrence(
                schedule=schedule,
                next_due_at=schedule,
                series_schedule=schedule,
                status=OccurrenceStatus.pending,
            )
        )
        self.db.add(reminder)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateReminderError("an active reminder with the same title and schedule already exists") from exc
        self.db.refresh(reminder)
        return reminder

    def create_from_local_parts(
        self,
        user_id: uuid.UUID,
        *,
        title: str,
        schedule_date: date,
        schedule_time: time,
        recurrence_frequency: RecurrenceFrequency | None,
        recurrence_interval: int = 1,
    ) -> Reminder:
        user = self.users.get(user_id)
        local_schedule = datetime.combine(
            schedule_date, schedule_time, tzinfo=ZoneInfo(user.timezone)
        )
        if recurrence_frequency is not None:
            local_now = datetime.now(ZoneInfo(user.timezone))
            while local_schedule <= local_now:
                local_schedule = next_occurrence(local_schedule, recurrence_frequency, recurrence_interval)
        return self.create(
            user_id,
            ReminderCreate(
                title=title,
                schedule=local_schedule,
                is_recurring=recurrence_frequency is not None,
                recurrence_frequency=recurrence_frequency,
                recurrence_interval=recurrence_interval,
            ),
        )

    def complete_occurrence(
        self, user_id: uuid.UUID, reminder_id: uuid.UUID, occurrence_id: uuid.UUID
    ) -> Reminder:
        user = self.users.get(user_id)
        reminder = self.db.scalar(
            select(Reminder)
            .options(selectinload(Reminder.occurrences))
            .where(Reminder.id == reminder_id, Reminder.user_id == user_id)
        )
        if reminder is None:
            raise LookupError("reminder not found")
        occurrence = next((item for item in reminder.occurrences if item.id == occurrence_id), None)
        if occurrence is None:
            raise LookupError("occurrence not found")
        if occurrence.status in {OccurrenceStatus.completed, OccurrenceStatus.canceled}:
            raise ValueError("occurrence is no longer pending")

        occurrence.status = OccurrenceStatus.completed
        if reminder.is_recurring and reminder.recurrence_frequency:
            next_record = advance_recurring_occurrence(reminder, occurrence, user.timezone)
            reminder.schedule = next_record.schedule
        else:
            reminder.status = ReminderStatus.completed
        self.db.commit()
        self.db.refresh(reminder)
        return reminder

    def reschedule(self, user_id: uuid.UUID, reminder_id: uuid.UUID, request):
        from app.reminders.rescheduling import ReminderRescheduler

        return ReminderRescheduler(self.db).reschedule(user_id, reminder_id, request)

    def cancel(self, user_id: uuid.UUID, reminder_id: uuid.UUID, request):
        from app.reminders.canceling import ReminderCanceler

        return ReminderCanceler(self.db).cancel(user_id, reminder_id, request)

    def snooze(
        self,
        user_id: uuid.UUID,
        reminder_id: uuid.UUID,
        occurrence_id: uuid.UUID,
        request,
    ):
        from app.reminders.snoozing import ReminderSnoozer

        return ReminderSnoozer(self.db).snooze(user_id, reminder_id, occurrence_id, request)

    def list(self, user_id: uuid.UUID, query: ReminderQuery) -> list[Reminder]:
        user = self.users.get(user_id)
        statement = (
            select(Reminder)
            .options(selectinload(Reminder.occurrences))
            .where(Reminder.user_id == user_id)
            .order_by(Reminder.schedule.asc())
        )
        if query.title:
            statement = statement.where(Reminder.title.ilike(f"%{query.title.strip()}%"))
        if query.start:
            statement = statement.where(Reminder.schedule >= to_utc(query.start, user.timezone))
        if query.end:
            statement = statement.where(Reminder.schedule <= to_utc(query.end, user.timezone))
        if query.occurrence_start or query.occurrence_end:
            occurrence_filters = [ReminderOccurrence.status == OccurrenceStatus.pending]
            if query.occurrence_start:
                occurrence_filters.append(
                    ReminderOccurrence.schedule >= to_utc(query.occurrence_start, user.timezone)
                )
            if query.occurrence_end:
                occurrence_filters.append(
                    ReminderOccurrence.schedule <= to_utc(query.occurrence_end, user.timezone)
                )
            statement = statement.where(Reminder.occurrences.any(*occurrence_filters))
        if query.status == ReminderListStatus.pending:
            statement = statement.where(
                Reminder.status == ReminderStatus.active,
                Reminder.occurrences.any(
                    ReminderOccurrence.status.in_([OccurrenceStatus.pending, OccurrenceStatus.notified])
                ),
            )
        elif query.status == ReminderListStatus.completed:
            statement = statement.where(
                or_(
                    Reminder.status == ReminderStatus.completed,
                    Reminder.occurrences.any(ReminderOccurrence.status == OccurrenceStatus.completed),
                )
            )
        return list(self.db.scalars(statement).unique().all())

    def list_with_occurrences(
        self, user_id: uuid.UUID, query: ReminderQuery
    ) -> list[tuple[Reminder, ReminderOccurrence | None]]:
        """List reminders with only the relevant pending occurrence loaded.

        This is used by agent discovery so a recurring reminder's full occurrence
        history is not loaded merely to resolve one requested date.
        """
        user = self.users.get(user_id)
        statement = select(Reminder).where(Reminder.user_id == user_id).order_by(Reminder.schedule.asc())
        if query.title:
            statement = statement.where(Reminder.title.ilike(f"%{query.title.strip()}%"))
        if query.start:
            statement = statement.where(Reminder.schedule >= to_utc(query.start, user.timezone))
        if query.end:
            statement = statement.where(Reminder.schedule <= to_utc(query.end, user.timezone))
        if query.occurrence_start or query.occurrence_end:
            occurrence_filters = [ReminderOccurrence.status == OccurrenceStatus.pending]
            if query.occurrence_start:
                occurrence_filters.append(
                    ReminderOccurrence.schedule >= to_utc(query.occurrence_start, user.timezone)
                )
            if query.occurrence_end:
                occurrence_filters.append(
                    ReminderOccurrence.schedule <= to_utc(query.occurrence_end, user.timezone)
                )
            statement = statement.where(Reminder.occurrences.any(*occurrence_filters))
        if query.status == ReminderListStatus.pending:
            statement = statement.where(
                Reminder.status == ReminderStatus.active,
                Reminder.occurrences.any(
                    ReminderOccurrence.status.in_([OccurrenceStatus.pending, OccurrenceStatus.notified])
                ),
            )
        elif query.status == ReminderListStatus.completed:
            statement = statement.where(
                or_(
                    Reminder.status == ReminderStatus.completed,
                    Reminder.occurrences.any(ReminderOccurrence.status == OccurrenceStatus.completed),
                )
            )
        reminders = list(self.db.scalars(statement).all())
        if not reminders:
            return []

        occurrence_statuses = [OccurrenceStatus.pending, OccurrenceStatus.notified]
        if query.status == ReminderListStatus.completed:
            occurrence_statuses = [OccurrenceStatus.completed]
        if query.occurrence_start or query.occurrence_end:
            occurrence_statuses = [OccurrenceStatus.pending]
        ranked_occurrences = select(
            ReminderOccurrence.id.label("occurrence_id"),
            func.row_number()
            .over(
                partition_by=ReminderOccurrence.reminder_id,
                order_by=ReminderOccurrence.schedule.asc(),
            )
            .label("occurrence_rank"),
        ).where(
            ReminderOccurrence.reminder_id.in_([item.id for item in reminders]),
            ReminderOccurrence.status.in_(occurrence_statuses),
        )
        if query.occurrence_start:
            ranked_occurrences = ranked_occurrences.where(
                ReminderOccurrence.schedule >= to_utc(query.occurrence_start, user.timezone)
            )
        if query.occurrence_end:
            ranked_occurrences = ranked_occurrences.where(
                ReminderOccurrence.schedule <= to_utc(query.occurrence_end, user.timezone)
            )
        ranked_subquery = ranked_occurrences.subquery()
        occurrence_statement = select(ReminderOccurrence).join(
            ranked_subquery,
            ReminderOccurrence.id == ranked_subquery.c.occurrence_id,
        ).where(ranked_subquery.c.occurrence_rank == 1)
        occurrences = list(self.db.scalars(occurrence_statement).all())
        by_reminder: dict[uuid.UUID, ReminderOccurrence] = {}
        for occurrence in occurrences:
            by_reminder.setdefault(occurrence.reminder_id, occurrence)
        return [(item, by_reminder.get(item.id)) for item in reminders]

