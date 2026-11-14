from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import RecurrenceFrequency
from app.recurrence.schedule import next_occurrence


def test_recurrence_advances_daily_and_weekly():
    schedule = datetime(2026, 11, 15, 9, 0, tzinfo=ZoneInfo("UTC"))
    assert next_occurrence(schedule, RecurrenceFrequency.daily).day == 16
    assert next_occurrence(schedule, RecurrenceFrequency.weekly).day == 22


def test_monthly_recurrence_clamps_to_last_day():
    schedule = datetime(2026, 1, 31, 9, 0, tzinfo=ZoneInfo("UTC"))
    result = next_occurrence(schedule, RecurrenceFrequency.monthly)
    assert result.year == 2026
    assert result.month == 2
    assert result.day == 28
