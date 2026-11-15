from datetime import datetime, timezone

from app.models import OccurrenceStatus, Reminder, ReminderOccurrence
from app.recurrence.schedule import next_occurrence
from app.time_utils import to_utc, to_user_timezone


def advance_recurring_occurrence(
    reminder: Reminder,
    occurrence: ReminderOccurrence,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> ReminderOccurrence:
    """Create or reuse the next nominal occurrence after a terminal occurrence."""
    if not reminder.is_recurring or reminder.recurrence_frequency is None:
        raise ValueError("only recurring reminders have subsequent occurrences")
    now = now or datetime.now(timezone.utc)
    nominal_schedule = occurrence.series_schedule or occurrence.schedule
    local_schedule = to_user_timezone(nominal_schedule, timezone_name)
    assert local_schedule is not None
    next_local = next_occurrence(
        local_schedule, reminder.recurrence_frequency, reminder.recurrence_interval
    )
    while to_utc(next_local, timezone_name) <= now:
        next_local = next_occurrence(
            next_local, reminder.recurrence_frequency, reminder.recurrence_interval
        )
    next_schedule = to_utc(next_local, timezone_name)
    existing = next(
        (
            item
            for item in reminder.occurrences
            if item.series_schedule is not None
            and to_utc(item.series_schedule, timezone_name) == next_schedule
            and item.status in {OccurrenceStatus.pending, OccurrenceStatus.notified}
        ),
        None,
    )
    if existing is not None:
        return existing
    next_occurrence_record = ReminderOccurrence(
        schedule=next_schedule,
        next_due_at=next_schedule,
        series_schedule=next_schedule,
        status=OccurrenceStatus.pending,
    )
    reminder.occurrences.append(next_occurrence_record)
    return next_occurrence_record
