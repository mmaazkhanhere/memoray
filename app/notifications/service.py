from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import Session

from app.models import (
    ChannelIdentity,
    ChannelIdentityStatus,
    DeliveryChannel,
    DeliveryDestination,
    NotificationAttempt,
    NotificationDelivery,
    NotificationStatus,
    OccurrenceStatus,
    Reminder,
    ReminderOccurrence,
    ReminderStatus,
)


class NotificationService:
    """Owns durable notification rows and their database state transitions."""

    def __init__(self, db: Session):
        self.db = db

    def schedule(
        self,
        reminder: Reminder,
        occurrence: ReminderOccurrence,
        *,
        content: str | None = None,
        identity: ChannelIdentity | None = None,
        channel: DeliveryChannel | None = None,
        destination_snapshot: str | None = None,
        destination_id: uuid.UUID | None = None,
        next_attempt_at: datetime | None = None,
        delivery_sequence: int | None = None,
    ) -> NotificationDelivery:
        if occurrence.status != OccurrenceStatus.pending:
            raise ValueError("only pending occurrences can be scheduled")
        if reminder.status != ReminderStatus.active:
            raise ValueError("only active reminders can be scheduled")
        if identity is not None and identity.status != ChannelIdentityStatus.linked:
            raise ValueError("channel identity is not linked")
        selected_channel = channel or (
            DeliveryChannel(identity.channel.value) if identity is not None else DeliveryChannel.in_app
        )
        selected_destination = destination_snapshot or (
            identity.external_destination_id if identity is not None else f"user:{reminder.user_id}"
        )
        row = NotificationDelivery(
            reminder_id=reminder.id,
            occurrence_id=occurrence.id,
            channel_identity_id=identity.id if identity else None,
            channel=selected_channel,
            destination_snapshot=selected_destination,
            destination_id=destination_id,
            content=content or reminder.title,
            provider_idempotency_key=f"notification:{uuid.uuid4()}",
            delivery_sequence=delivery_sequence or self._next_sequence(occurrence.id),
            status=NotificationStatus.pending,
            next_attempt_at=next_attempt_at or occurrence.next_due_at,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _next_sequence(self, occurrence_id: uuid.UUID) -> int:
        last = self.db.scalar(
            select(NotificationDelivery.delivery_sequence)
            .where(NotificationDelivery.occurrence_id == occurrence_id)
            .order_by(NotificationDelivery.delivery_sequence.desc())
            .limit(1)
        )
        return (last or 0) + 1

    def cancel_for_occurrence(self, occurrence_id: uuid.UUID) -> int:
        result = self.db.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.occurrence_id == occurrence_id,
                NotificationDelivery.status.in_(
                    [NotificationStatus.pending, NotificationStatus.sending]
                ),
            )
            .values(
                status=NotificationStatus.canceled,
                worker_id=None,
                lease_expires_at=None,
                last_error="occurrence canceled",
            )
        )
        return result.rowcount or 0

    def cancel_for_identity(self, identity_id: uuid.UUID, reason: str) -> int:
        result = self.db.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.channel_identity_id == identity_id,
                NotificationDelivery.status.in_(
                    [NotificationStatus.pending, NotificationStatus.sending]
                ),
            )
            .values(
                status=NotificationStatus.canceled,
                worker_id=None,
                lease_expires_at=None,
                last_error=reason[:2000],
            )
        )
        return result.rowcount or 0

    def replace_pending(
        self,
        reminder: Reminder,
        occurrence: ReminderOccurrence,
        *,
        content: str | None = None,
        identity: ChannelIdentity | None = None,
        channel: DeliveryChannel | None = None,
        destination_snapshot: str | None = None,
        destination_id: uuid.UUID | None = None,
    ) -> NotificationDelivery:
        """Cancel an unsent intent and create an immutable replacement."""
        self.cancel_for_occurrence(occurrence.id)
        return self.schedule(
            reminder,
            occurrence,
            content=content,
            identity=identity,
            channel=channel,
            destination_snapshot=destination_snapshot,
            destination_id=destination_id,
        )

    def recover_expired(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        rows = list(
            self.db.scalars(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.status == NotificationStatus.sending,
                    NotificationDelivery.lease_expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            row.status = NotificationStatus.unknown
            row.last_error = "worker lease expired before delivery result was recorded"
            row.worker_id = None
            row.lease_expires_at = None
            self._finish_attempt(row, "unknown", row.last_error)
        self.db.commit()
        return len(rows)

    def claim_due(
        self,
        worker_id: str,
        *,
        limit: int = 100,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> list[NotificationDelivery]:
        now = now or datetime.now(timezone.utc)
        rows = list(
            self.db.scalars(
                select(NotificationDelivery)
                .join(Reminder, Reminder.id == NotificationDelivery.reminder_id)
                .join(ReminderOccurrence, ReminderOccurrence.id == NotificationDelivery.occurrence_id)
                .where(
                    NotificationDelivery.status == NotificationStatus.pending,
                    NotificationDelivery.next_attempt_at <= now,
                    Reminder.status == ReminderStatus.active,
                    ReminderOccurrence.status == OccurrenceStatus.pending,
                    or_(
                        NotificationDelivery.channel_identity_id.is_(None),
                        exists().where(
                            ChannelIdentity.id == NotificationDelivery.channel_identity_id,
                            ChannelIdentity.status == ChannelIdentityStatus.linked,
                        ),
                    ),
                    or_(
                        NotificationDelivery.destination_id.is_(None),
                        exists().where(
                            DeliveryDestination.id == NotificationDelivery.destination_id,
                            DeliveryDestination.is_linked.is_(True),
                            DeliveryDestination.is_valid.is_(True),
                        ),
                    ),
                )
                .order_by(NotificationDelivery.next_attempt_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        lease = now + timedelta(seconds=lease_seconds)
        for row in rows:
            row.status = NotificationStatus.sending
            row.worker_id = worker_id
            row.lease_expires_at = lease
            row.last_attempt_at = now
            row.attempt_count += 1
            row.last_error = None
            row.attempts.append(
                NotificationAttempt(
                    sequence=row.attempt_count,
                    worker_id=worker_id,
                    started_at=now,
                )
            )
        self.db.commit()
        return rows

    def finalize(
        self,
        notification_id: uuid.UUID,
        worker_id: str,
        *,
        classification: str,
        error: str | None = None,
        provider_reference: str | None = None,
        retry_after: float | None = None,
        invalidate_identity: bool = False,
        max_attempts: int = 3,
        uncertain_retry_limit: int = 2,
        max_delay_seconds: int = 300,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(timezone.utc)
        row = self.db.scalar(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.id == notification_id,
                NotificationDelivery.status == NotificationStatus.sending,
                NotificationDelivery.worker_id == worker_id,
            )
            .with_for_update()
        )
        if row is None:
            self.db.rollback()
            return False
        occurrence = self.db.get(ReminderOccurrence, row.occurrence_id)
        reminder = self.db.get(Reminder, row.reminder_id)
        eligible = bool(
            occurrence
            and reminder
            and occurrence.status == OccurrenceStatus.pending
            and reminder.status == ReminderStatus.active
        )
        if not eligible:
            row.status = NotificationStatus.canceled
            row.last_error = "occurrence became ineligible while sending"
            row.worker_id = None
            row.lease_expires_at = None
            self._finish_attempt(row, "canceled", row.last_error, now=now)
            self.db.commit()
            return True
        if classification == "success":
            row.status = NotificationStatus.sent
            row.sent_at = now
            row.last_error = None
            row.worker_id = None
            row.lease_expires_at = None
            occurrence.status = OccurrenceStatus.notified
        elif classification == "permanent":
            row.status = NotificationStatus.failed
            row.last_error = (error or "permanent provider failure")[:2000]
            row.worker_id = None
            row.lease_expires_at = None
            if invalidate_identity and row.channel_identity_id:
                identity = self.db.get(ChannelIdentity, row.channel_identity_id)
                if identity:
                    identity.status = ChannelIdentityStatus.invalid
                    self.cancel_for_identity(identity.id, row.last_error)
        elif classification == "unknown":
            row.uncertain_attempt_count += 1
            if row.uncertain_attempt_count >= uncertain_retry_limit + 1:
                row.status = NotificationStatus.unknown
                row.last_error = (error or "provider result was ambiguous")[:2000]
                row.worker_id = None
                row.lease_expires_at = None
            else:
                row.status = NotificationStatus.pending
                row.next_attempt_at = now + timedelta(
                    seconds=self._retry_delay(row.attempt_count, retry_after, max_delay_seconds)
                )
                row.last_error = (error or "ambiguous provider result; retrying")[:2000]
                row.worker_id = None
                row.lease_expires_at = None
        else:
            if row.attempt_count >= max_attempts:
                row.status = NotificationStatus.failed
                row.last_error = (error or "retry budget exhausted")[:2000]
                row.worker_id = None
                row.lease_expires_at = None
            else:
                row.status = NotificationStatus.pending
                row.next_attempt_at = now + timedelta(
                    seconds=self._retry_delay(row.attempt_count, retry_after, max_delay_seconds)
                )
                row.last_error = (error or "transient provider failure")[:2000]
                row.worker_id = None
                row.lease_expires_at = None
        self._finish_attempt(row, classification, error, provider_reference, now)
        self.db.commit()
        return True

    @staticmethod
    def _retry_delay(attempt_count: int, retry_after: float | None, maximum: int) -> float:
        if retry_after is not None:
            return max(0.0, min(float(retry_after), float(maximum)))
        base = min(float(maximum), 30.0 * (2 ** max(attempt_count - 1, 0)))
        return min(float(maximum), base * random.uniform(0.8, 1.2))

    @staticmethod
    def _finish_attempt(
        row: NotificationDelivery,
        classification: str,
        error: str | None = None,
        provider_reference: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if row.attempts:
            attempt = row.attempts[-1]
            attempt.finished_at = now or datetime.now(timezone.utc)
            attempt.classification = classification
            attempt.error = error[:2000] if error else None
            attempt.provider_reference = provider_reference
