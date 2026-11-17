from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import Settings, get_settings
from app.models import OccurrenceStatus, ReminderOccurrence


def external_event_id(occurrence_id: uuid.UUID) -> str:
    """Google event IDs use lower-case base32hex characters."""
    encoded = base64.b32hexencode(occurrence_id.bytes).decode().lower().rstrip("=")
    return f"m{encoded}"


def event_body(
    occurrence: ReminderOccurrence,
    *,
    timezone_name: str,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()
    start = occurrence.next_due_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    local_start = start.astimezone(ZoneInfo(timezone_name))
    local_end = local_start + timedelta(minutes=settings.google_calendar_event_duration_minutes)
    return {
        "summary": f"Reminder: {occurrence.reminder.title}",
        "description": "Created by Memoray. Manage this reminder in Memoray.",
        "start": {"dateTime": local_start.isoformat(), "timeZone": timezone_name},
        "end": {"dateTime": local_end.isoformat(), "timeZone": timezone_name},
        "extendedProperties": {
            "private": {
                "memorayReminderId": str(occurrence.reminder_id),
                "memorayOccurrenceId": str(occurrence.id),
            }
        },
    }


def should_exist(occurrence: ReminderOccurrence) -> bool:
    return occurrence.status in {OccurrenceStatus.pending, OccurrenceStatus.notified}
