from app.models import Reminder
from app.schemas import OccurrenceResponse, ReminderResponse
from app.time_utils import to_user_timezone


def to_response(reminder: Reminder, timezone_name: str) -> ReminderResponse:
    occurrence = reminder.occurrences[-1] if reminder.occurrences else None
    occurrence_response = None
    if occurrence:
        occurrence_response = OccurrenceResponse(
            id=occurrence.id,
            schedule=to_user_timezone(occurrence.schedule, timezone_name),
            next_due_at=to_user_timezone(occurrence.next_due_at, timezone_name),
            status=occurrence.status,
        )
    return ReminderResponse(
        id=reminder.id,
        user_id=reminder.user_id,
        title=reminder.title,
        schedule=to_user_timezone(reminder.schedule, timezone_name),
        is_recurring=reminder.is_recurring,
        recurrence_rule=reminder.recurrence_rule,
        status=reminder.status,
        occurrence=occurrence_response,
    )

