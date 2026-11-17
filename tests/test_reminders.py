from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import OccurrenceStatus, RecurrenceFrequency, ReminderStatus
from app.reminders.errors import DuplicateReminderError, ReminderInPastError
from app.reminders.service import ReminderService
from app.schemas import ReminderCreate, ReminderListStatus, ReminderQuery, UserCreate
from app.users.service import UserService


def test_create_stores_utc_and_pending_occurrence(db):
    user = UserService(db).create(UserCreate(name="A", timezone="Asia/Karachi"))
    schedule = (datetime.now(ZoneInfo("Asia/Karachi")) + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0, tzinfo=None
    )
    reminder = ReminderService(db).create(
        user.id, ReminderCreate(title="Call", schedule=schedule)
    )
    assert reminder.schedule.hour == 4
    assert reminder.status == ReminderStatus.active
    assert reminder.occurrences[0].status == OccurrenceStatus.pending


def test_list_filters_by_title_and_status(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    first = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    second = (datetime.now(timezone.utc) + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    service.create(user.id, ReminderCreate(title="Pay bill", schedule=first))
    service.create(user.id, ReminderCreate(title="Call", schedule=second))
    results = service.list(user.id, ReminderQuery(title="bill", status=ReminderListStatus.pending))
    assert [item.title for item in results] == ["Pay bill"]


def test_user_isolation(db):
    first = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    second = UserService(db).create(UserCreate(name="B", timezone="UTC"))
    schedule = datetime.now(timezone.utc) + timedelta(days=1)
    ReminderService(db).create(first.id, ReminderCreate(title="Private", schedule=schedule))
    assert ReminderService(db).list(second.id, ReminderQuery()) == []


def test_past_and_duplicate_reminders_are_rejected(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    with pytest.raises(ReminderInPastError):
        service.create(user.id, ReminderCreate(title="Past", schedule=datetime.now(timezone.utc)))
    schedule = datetime.now(timezone.utc) + timedelta(hours=1)
    service.create(user.id, ReminderCreate(title="Same task", schedule=schedule))
    with pytest.raises(DuplicateReminderError):
        service.create(user.id, ReminderCreate(title="same TASK", schedule=schedule))


def test_recurring_reminder_advances_to_a_new_pending_occurrence(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    reminder = service.create(
        user.id,
        ReminderCreate(
            title="Daily check",
            schedule=datetime.now(timezone.utc) + timedelta(hours=1),
            is_recurring=True,
            recurrence_frequency=RecurrenceFrequency.daily,
        ),
    )
    first_occurrence = reminder.occurrences[0]
    updated = service.complete_occurrence(user.id, reminder.id, first_occurrence.id)
    assert updated.status == ReminderStatus.active
    assert len(updated.occurrences) == 2
    assert updated.occurrences[-1].status == OccurrenceStatus.pending
    assert updated.occurrences[-1].schedule > first_occurrence.schedule


def test_recurring_start_today_after_time_has_passed_rolls_to_next_occurrence(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    local_now = datetime.now(ZoneInfo("UTC"))
    reminder = service.create_from_local_parts(
        user.id,
        title="Take medicine",
        schedule_date=local_now.date(),
        schedule_time=(local_now - timedelta(hours=1)).time().replace(microsecond=0),
        recurrence_frequency=RecurrenceFrequency.daily,
    )
    assert reminder.schedule.date() > local_now.date()

