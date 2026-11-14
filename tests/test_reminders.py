from datetime import datetime, timedelta, timezone

import pytest

from app.models import OccurrenceStatus, ReminderStatus
from app.reminders.errors import DuplicateReminderError, ReminderInPastError
from app.reminders.service import ReminderService
from app.schemas import ReminderCreate, ReminderListStatus, ReminderQuery, UserCreate
from app.users.service import UserService


def test_create_stores_utc_and_pending_occurrence(db):
    user = UserService(db).create(UserCreate(name="A", timezone="Asia/Karachi"))
    reminder = ReminderService(db).create(
        user.id, ReminderCreate(title="Call", schedule=datetime(2026, 11, 15, 9, 0))
    )
    assert reminder.schedule.hour == 4
    assert reminder.status == ReminderStatus.active
    assert reminder.occurrences[0].status == OccurrenceStatus.pending


def test_list_filters_by_title_and_status(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    service.create(user.id, ReminderCreate(title="Pay bill", schedule=datetime(2026, 11, 15, 9, 0)))
    service.create(user.id, ReminderCreate(title="Call", schedule=datetime(2026, 11, 16, 9, 0)))
    results = service.list(user.id, ReminderQuery(title="bill", status=ReminderListStatus.pending))
    assert [item.title for item in results] == ["Pay bill"]


def test_user_isolation(db):
    first = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    second = UserService(db).create(UserCreate(name="B", timezone="UTC"))
    ReminderService(db).create(first.id, ReminderCreate(title="Private", schedule=datetime(2026, 11, 15, 9, 0)))
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

