import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ChannelIdentity,
    ChannelIdentityStatus,
    ChannelKind,
    OccurrenceStatus,
    Reminder,
    ReminderOccurrence,
    ReminderStatus,
)
from app.notifications.service import NotificationService
from app.reminders.destinations import DestinationService, default_telegram_identity
from app.reminders.errors import (
    InvalidDestinationError,
    OccurrenceNotPendingError,
    ReminderInPastError,
    ReminderNotActiveError,
)
from app.reminders.changes import queue_occurrence_changes
from app.recurrence.schedule import next_occurrence
from app.schemas import ReminderRescheduleRequest, RescheduleScope
from app.time_utils import to_utc, to_user_timezone
from app.users.service import UserService


class ReminderRescheduler:
    def __init__(self, db: Session):
        self.db = db
        self.users = UserService(db)
        self.destinations = DestinationService(db)

    def reschedule(
        self,
        user_id: uuid.UUID,
        reminder_id: uuid.UUID,
        request: ReminderRescheduleRequest,
        *,
        notification_identity: ChannelIdentity | None = None,
    ) -> Reminder:
        user = self.users.get(user_id)
        schedule = to_utc(request.schedule, user.timezone)
        now = datetime.now(timezone.utc)
        if schedule <= now:
            raise ReminderInPastError("reminder schedule must be strictly in the future")

        reminder = self.db.scalar(
            select(Reminder)
            .options(selectinload(Reminder.occurrences))
            .where(Reminder.id == reminder_id, Reminder.user_id == user_id)
        )
        if reminder is None:
            raise LookupError("reminder not found")
        if reminder.status != ReminderStatus.active:
            raise ReminderNotActiveError("only active reminders can be rescheduled")

        destination_id = request.destination_id or reminder.destination_id
        if destination_id is None:
            raise InvalidDestinationError("an active linked destination is required")
        destination = self.destinations.get_owned_valid(user_id, destination_id)

        if reminder.is_recurring and request.scope is None:
            raise ValueError("recurring reminders require an explicit reschedule scope")
        scope = request.scope or RescheduleScope.series

        if scope == RescheduleScope.occurrence:
            if not reminder.is_recurring:
                raise ValueError("occurrence scope requires a recurring reminder")
            if request.occurrence_id is None:
                raise ValueError("occurrence scope requires an occurrence ID")
            occurrence = self._target_occurrence(reminder, request)
            if occurrence.status != OccurrenceStatus.pending:
                raise OccurrenceNotPendingError("only pending occurrences can be rescheduled")
            was_next = occurrence is min(
                (item for item in reminder.occurrences if item.status == OccurrenceStatus.pending),
                key=lambda item: item.schedule,
                default=None,
            )
            occurrence.schedule = schedule
            occurrence.next_due_at = schedule
            if was_next:
                reminder.schedule = schedule
        else:
            pending = sorted(
                (item for item in reminder.occurrences if item.status == OccurrenceStatus.pending),
                key=lambda item: item.schedule,
            )
            if not pending:
                raise OccurrenceNotPendingError("reminder has no pending occurrence")
            generated = schedule
            for index, pending_occurrence in enumerate(pending):
                if index:
                    local_generated = to_user_timezone(generated, user.timezone)
                    assert local_generated is not None
                    local_generated = next_occurrence(
                        local_generated,
                        reminder.recurrence_frequency,
                        reminder.recurrence_interval,
                    )
                    generated = to_utc(local_generated, user.timezone)
                pending_occurrence.schedule = generated
                pending_occurrence.next_due_at = generated
                pending_occurrence.series_schedule = generated
            reminder.schedule = schedule
            reminder.series_anchor_schedule = schedule

        reminder.destination_id = destination.id
        identity = (
            notification_identity
            if notification_identity is not None
            and notification_identity.channel == ChannelKind.telegram
            else None
        )
        if identity is not None and (
            identity.user_id != user_id or identity.status != ChannelIdentityStatus.linked
        ):
            raise InvalidDestinationError("notification identity is not linked")
        if request.destination_id is None:
            identity = identity or default_telegram_identity(self.db, user_id)
        for pending_occurrence in (
            [occurrence]
            if scope == RescheduleScope.occurrence
            else [item for item in reminder.occurrences if item.status == OccurrenceStatus.pending]
        ):
            NotificationService(self.db).replace_pending(
                reminder,
                pending_occurrence,
                content=f"Reminder: {reminder.title}",
                identity=identity,
                channel=None if identity else destination.channel,
                destination_id=destination.id,
                destination_snapshot=(
                    identity.external_destination_id if identity is not None else destination.identity
                ),
            )
        queue_occurrence_changes(
            self.db,
            [
                item.id
                for item in (
                    [occurrence]
                    if scope == RescheduleScope.occurrence
                    else [item for item in reminder.occurrences if item.status == OccurrenceStatus.pending]
                )
            ],
        )
        self.db.commit()
        self.db.refresh(reminder)
        return reminder

    @staticmethod
    def _target_occurrence(
        reminder: Reminder, request: ReminderRescheduleRequest
    ) -> ReminderOccurrence:
        if request.occurrence_id is not None:
            occurrence = next(
                (item for item in reminder.occurrences if item.id == request.occurrence_id), None
            )
            if occurrence is None:
                raise LookupError("occurrence not found")
            return occurrence
        pending = [item for item in reminder.occurrences if item.status == OccurrenceStatus.pending]
        if not pending:
            raise OccurrenceNotPendingError("reminder has no pending occurrence")
        return min(pending, key=lambda item: item.schedule)
