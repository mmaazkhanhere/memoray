import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import OccurrenceStatus, Reminder, ReminderOccurrence, ReminderStatus
from app.reminders.errors import DuplicateReminderError, ReminderInPastError
from app.schemas import ReminderCreate, ReminderListStatus, ReminderQuery
from app.time_utils import to_utc
from app.users.service import UserService


class ReminderService:
    def __init__(self, db: Session):
        self.db = db
        self.users = UserService(db)

    def create(self, user_id: uuid.UUID, data: ReminderCreate) -> Reminder:
        user = self.users.get(user_id)
        schedule = to_utc(data.schedule, user.timezone)
        if schedule <= datetime.now(timezone.utc):
            raise ReminderInPastError("reminder schedule must be in the future")
        normalized_title = data.title.strip().casefold()
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
            is_recurring=data.is_recurring,
            recurrence_rule=data.recurrence_rule,
            status=ReminderStatus.active,
        )
        reminder.occurrences.append(
            ReminderOccurrence(schedule=schedule, next_due_at=schedule, status=OccurrenceStatus.pending)
        )
        self.db.add(reminder)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateReminderError("an active reminder with the same title and schedule already exists") from exc
        self.db.refresh(reminder)
        return reminder

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

