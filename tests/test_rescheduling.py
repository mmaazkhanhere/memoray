from datetime import datetime, timedelta, timezone

import pytest

from app.models import OccurrenceStatus, RecurrenceFrequency, ReminderOccurrence, ReminderStatus
from app.reminders.errors import OccurrenceNotNotifiedError, ReminderNotActiveError
from app.reminders.service import ReminderService
from app.schemas import ReminderCreate, ReminderRescheduleRequest, RescheduleScope, SnoozeRequest, UserCreate
from app.users.service import UserService


def future(minutes=10):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def test_reschedule_moves_one_time_occurrence(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    reminder = ReminderService(db).create(user.id, ReminderCreate(title="Call", schedule=future()))
    moved_to = future(30)

    updated = ReminderService(db).reschedule(
        user.id,
        reminder.id,
        ReminderRescheduleRequest(schedule=moved_to),
    )

    assert updated.schedule == updated.occurrences[0].schedule
    assert updated.occurrences[0].next_due_at == updated.schedule


def test_snooze_requires_notified_occurrence_and_only_changes_due_time(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    reminder = ReminderService(db).create(user.id, ReminderCreate(title="Call", schedule=future()))
    occurrence = reminder.occurrences[0]
    with pytest.raises(OccurrenceNotNotifiedError):
        ReminderService(db).snooze(
            user.id, reminder.id, occurrence.id, SnoozeRequest(duration_seconds=60)
        )

    occurrence.status = OccurrenceStatus.notified
    db.commit()
    original_schedule = occurrence.schedule
    updated = ReminderService(db).snooze(
        user.id, reminder.id, occurrence.id, SnoozeRequest(duration_seconds=60)
    )
    assert updated.occurrences[0].status == OccurrenceStatus.pending
    assert updated.occurrences[0].schedule == original_schedule
    assert updated.occurrences[0].next_due_at != original_schedule


def test_completed_reminder_cannot_be_rescheduled(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    reminder = service.create(user.id, ReminderCreate(title="Call", schedule=future()))
    reminder.status = ReminderStatus.completed
    db.commit()
    with pytest.raises(ReminderNotActiveError):
        service.reschedule(
            user.id, reminder.id, ReminderRescheduleRequest(schedule=future(20))
        )


def test_recurring_instance_reschedule_preserves_series_cadence(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    reminder = service.create(
        user.id,
        ReminderCreate(
            title="Standup",
            schedule=future(10),
            is_recurring=True,
            recurrence_frequency=RecurrenceFrequency.daily,
        ),
    )
    original_series_time = reminder.occurrences[0].series_schedule
    moved_to = future(60)
    service.reschedule(
        user.id,
        reminder.id,
        ReminderRescheduleRequest(
            schedule=moved_to,
            scope=RescheduleScope.occurrence,
            occurrence_id=reminder.occurrences[0].id,
        ),
    )
    updated = service.complete_occurrence(user.id, reminder.id, reminder.occurrences[0].id)
    assert updated.occurrences[-1].schedule.date() == (original_series_time + timedelta(days=1)).date()


def test_recurring_reschedule_requires_explicit_scope(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    reminder = service.create(
        user.id,
        ReminderCreate(
            title="Medicine",
            schedule=future(10),
            is_recurring=True,
            recurrence_frequency=RecurrenceFrequency.daily,
        ),
    )
    original = reminder.schedule
    with pytest.raises(ValueError, match="explicit reschedule scope"):
        service.reschedule(user.id, reminder.id, ReminderRescheduleRequest(schedule=future(30)))
    assert reminder.schedule == original


def test_series_scope_shifts_all_pending_occurrences(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    reminder = service.create(
        user.id,
        ReminderCreate(
            title="Medicine",
            schedule=future(10),
            is_recurring=True,
            recurrence_frequency=RecurrenceFrequency.daily,
        ),
    )
    first = reminder.occurrences[0]
    second_schedule = first.schedule + timedelta(days=1)
    reminder.occurrences.append(
        ReminderOccurrence(
            schedule=second_schedule,
            next_due_at=second_schedule,
            series_schedule=second_schedule,
            status=OccurrenceStatus.pending,
        )
    )
    db.commit()
    new_schedule = future(60)
    updated = service.reschedule(
        user.id,
        reminder.id,
        ReminderRescheduleRequest(schedule=new_schedule, scope=RescheduleScope.series),
    )
    schedules = sorted(item.schedule for item in updated.occurrences if item.status == OccurrenceStatus.pending)
    assert schedules[0] == new_schedule.replace(tzinfo=None)
    assert schedules[1].date() == (new_schedule + timedelta(days=1)).date()
