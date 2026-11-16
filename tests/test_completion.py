from datetime import datetime, timedelta, timezone

from app.models import OccurrenceStatus, RecurrenceFrequency, ReminderStatus
from app.reminders.completion import CompletionService
from app.reminders.service import ReminderService
from app.schemas import ReminderCreate, UserCreate
from app.users.service import UserService


def future(hours=1):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def delivered(reminder):
    occurrence = reminder.occurrences[0]
    occurrence.status = OccurrenceStatus.notified
    return occurrence


def test_one_time_completion_is_idempotent(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    reminder = ReminderService(db).create(
        user.id, ReminderCreate(title="Pay bill", schedule=future())
    )
    occurrence = delivered(reminder)
    db.commit()

    service = CompletionService(db)
    first = service.complete_by_id(user.id, occurrence.id)
    db.commit()
    second = service.complete_by_id(user.id, occurrence.id)

    assert first.operation == "completed"
    assert second.operation == "already_completed"
    assert reminder.status == ReminderStatus.completed
    assert occurrence.status == OccurrenceStatus.completed


def test_recurring_completion_advances_only_the_series(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    reminder = ReminderService(db).create(
        user.id,
        ReminderCreate(
            title="Standup",
            schedule=future(),
            is_recurring=True,
            recurrence_frequency=RecurrenceFrequency.daily,
        ),
    )
    occurrence = delivered(reminder)
    db.commit()

    result = CompletionService(db).complete_by_id(user.id, occurrence.id)
    db.commit()

    assert result.operation == "completed"
    assert reminder.status == ReminderStatus.active
    assert occurrence.status == OccurrenceStatus.completed
    assert len(reminder.occurrences) == 2
    assert result.successor.status == OccurrenceStatus.pending


def test_ambiguous_matching_completion_does_not_mutate(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    first = ReminderService(db).create(
        user.id, ReminderCreate(title="Call", schedule=future())
    )
    second = ReminderService(db).create(
        user.id, ReminderCreate(title="Call again", schedule=future(2))
    )
    delivered(first)
    delivered(second)
    db.commit()

    result = CompletionService(db).complete_matching(user.id, title="Call")

    assert result.operation == "ambiguous"
    assert first.occurrences[0].status == OccurrenceStatus.notified
    assert second.occurrences[0].status == OccurrenceStatus.notified
