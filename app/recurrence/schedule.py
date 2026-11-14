import calendar
from datetime import datetime, timedelta

from app.models import RecurrenceFrequency


def next_occurrence(
    local_schedule: datetime, frequency: RecurrenceFrequency, interval: int = 1
) -> datetime:
    """Return the next local occurrence while preserving wall-clock time."""
    if interval < 1:
        raise ValueError("recurrence interval must be at least 1")
    if frequency == RecurrenceFrequency.daily:
        return local_schedule + timedelta(days=interval)
    if frequency == RecurrenceFrequency.weekly:
        return local_schedule + timedelta(weeks=interval)
    if frequency == RecurrenceFrequency.monthly:
        month_index = local_schedule.year * 12 + local_schedule.month - 1 + interval
        year, month_index = divmod(month_index, 12)
        month = month_index + 1
        day = min(local_schedule.day, calendar.monthrange(year, month)[1])
        return local_schedule.replace(year=year, month=month, day=day)
    if frequency == RecurrenceFrequency.yearly:
        year = local_schedule.year + interval
        day = min(local_schedule.day, calendar.monthrange(year, local_schedule.month)[1])
        return local_schedule.replace(year=year, day=day)
    raise ValueError(f"unsupported recurrence frequency: {frequency}")

