from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelIdentityStatus,
    OccurrenceStatus,
    PhoneLink,
    Reminder,
    ReminderOccurrence,
    ReminderStatus,
    UrgentCallAttempt,
    UrgentCallStatus,
    UrgentDeliveryStatus,
    UrgentReminderDelivery,
)
from app.notifications.service import NotificationService
from app.reminders.destinations import default_telegram_identity


class UrgentReminderService:
    """Owns urgent reminder workflow state; provider calls happen outside DB transactions."""

    def __init__(self, db: Session, *, retry_delay_seconds: int = 30):
        self.db = db
        self.retry_delay_seconds = retry_delay_seconds

    def plan(self, reminder: Reminder, occurrence: ReminderOccurrence, *, telegram_identity=None) -> UrgentReminderDelivery:
        if not reminder.is_urgent:
            raise ValueError("only urgent reminders can use the urgent delivery workflow")
        phone = self.db.scalar(
            select(PhoneLink)
            .where(PhoneLink.user_id == reminder.user_id, PhoneLink.is_active.is_(True))
            .order_by(PhoneLink.created_at.asc())
        )
        telegram_identity = telegram_identity or default_telegram_identity(self.db, reminder.user_id)
        if phone is None:
            raise ValueError("urgent reminders require an active linked phone number")
        if telegram_identity is None or telegram_identity.status != ChannelIdentityStatus.linked:
            raise ValueError("urgent reminders require a linked Telegram account")
        existing = self.db.scalar(
            select(UrgentReminderDelivery).where(UrgentReminderDelivery.occurrence_id == occurrence.id)
        )
        if existing is not None:
            if existing.status in {
                UrgentDeliveryStatus.canceled,
                UrgentDeliveryStatus.delivered,
                UrgentDeliveryStatus.telegram_queued,
            }:
                existing.status = UrgentDeliveryStatus.pending
                existing.attempt_count = 0
                existing.next_attempt_at = occurrence.next_due_at
                existing.worker_id = None
                existing.lease_expires_at = None
                existing.last_error = None
                existing.attempts.clear()
            return existing
        row = UrgentReminderDelivery(
            reminder_id=reminder.id,
            occurrence_id=occurrence.id,
            phone_snapshot=phone.e164_number,
            telegram_snapshot=telegram_identity.external_destination_id,
            status=UrgentDeliveryStatus.pending,
            next_attempt_at=occurrence.next_due_at,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def cancel_for_occurrence(self, occurrence_id: uuid.UUID) -> int:
        result = self.db.query(UrgentReminderDelivery).filter(
            UrgentReminderDelivery.occurrence_id == occurrence_id,
            UrgentReminderDelivery.status.in_(
                [UrgentDeliveryStatus.pending, UrgentDeliveryStatus.calling, UrgentDeliveryStatus.retry_pending]
            ),
        ).update(
            {
                UrgentReminderDelivery.status: UrgentDeliveryStatus.canceled,
                UrgentReminderDelivery.worker_id: None,
                UrgentReminderDelivery.lease_expires_at: None,
                UrgentReminderDelivery.last_error: "occurrence canceled",
            },
            synchronize_session=False,
        )
        return int(result or 0)

    def recover_expired(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        rows = list(
            self.db.scalars(
                select(UrgentReminderDelivery).where(
                    UrgentReminderDelivery.status == UrgentDeliveryStatus.calling,
                    UrgentReminderDelivery.lease_expires_at <= now,
                ).with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            row.status = UrgentDeliveryStatus.retry_pending
            row.worker_id = None
            row.lease_expires_at = None
            row.next_attempt_at = now
            row.last_error = "call worker lease expired before provider result"
        self.db.commit()
        return len(rows)

    def claim_due(
        self,
        worker_id: str,
        *,
        limit: int = 20,
        lease_seconds: int = 90,
        now: datetime | None = None,
    ) -> list[tuple[UrgentReminderDelivery, UrgentCallAttempt]]:
        now = now or datetime.now(timezone.utc)
        rows = list(
            self.db.scalars(
                select(UrgentReminderDelivery)
                .join(Reminder, Reminder.id == UrgentReminderDelivery.reminder_id)
                .join(ReminderOccurrence, ReminderOccurrence.id == UrgentReminderDelivery.occurrence_id)
                .where(
                    UrgentReminderDelivery.status.in_([
                        UrgentDeliveryStatus.pending,
                        UrgentDeliveryStatus.retry_pending,
                    ]),
                    UrgentReminderDelivery.next_attempt_at <= now,
                    UrgentReminderDelivery.attempt_count < 2,
                    Reminder.status == ReminderStatus.active,
                    ReminderOccurrence.status == OccurrenceStatus.pending,
                )
                .order_by(UrgentReminderDelivery.next_attempt_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        lease = now + timedelta(seconds=lease_seconds)
        claimed: list[tuple[UrgentReminderDelivery, UrgentCallAttempt]] = []
        for row in rows:
            row.status = UrgentDeliveryStatus.calling
            row.worker_id = worker_id
            row.lease_expires_at = lease
            row.attempt_count += 1
            attempt = UrgentCallAttempt(
                delivery_id=row.id,
                attempt_number=row.attempt_count,
                # The attempt number tracks user-visible call tries. A
                # transport failure removes the provisional row before retry,
                # preserving this stable provider idempotency key.
                correlation_key=f"urgent:{row.id}:attempt:{row.attempt_count}",
                status=UrgentCallStatus.dispatching,
            )
            self.db.add(attempt)
            claimed.append((row, attempt))
        self.db.commit()
        return claimed

    def record_call_started(self, correlation_key: str, external_call_id: str) -> bool:
        attempt = self.db.scalar(
            select(UrgentCallAttempt).where(UrgentCallAttempt.correlation_key == correlation_key).with_for_update()
        )
        if attempt is None:
            return False
        if attempt.external_call_id and attempt.external_call_id != external_call_id:
            return False
        attempt.external_call_id = external_call_id
        if attempt.status == UrgentCallStatus.dispatching:
            attempt.status = UrgentCallStatus.ringing
        delivery = self.db.get(UrgentReminderDelivery, attempt.delivery_id)
        if delivery:
            delivery.worker_id = None
            # Keep a callback lease after the provider accepts the call. If
            # Vapi never reports an outcome, recovery can schedule a safe retry.
            delivery.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=300)
        self.db.commit()
        return True

    def record_dispatch_failure(self, correlation_key: str, error: str) -> bool:
        attempt = self.db.scalar(
            select(UrgentCallAttempt).where(UrgentCallAttempt.correlation_key == correlation_key).with_for_update()
        )
        if attempt is None:
            return False
        attempt.status = UrgentCallStatus.failed
        attempt.last_error = error[:2000]
        delivery = self.db.get(UrgentReminderDelivery, attempt.delivery_id)
        if delivery and delivery.status == UrgentDeliveryStatus.calling:
            self.db.delete(attempt)
            delivery.attempt_count = max(0, delivery.attempt_count - 1)
            delivery.status = UrgentDeliveryStatus.retry_pending
            delivery.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=self.retry_delay_seconds)
            delivery.worker_id = None
            delivery.lease_expires_at = None
            delivery.last_error = error[:2000]
        self.db.commit()
        return True

    def handle_provider_outcome(
        self,
        *,
        external_call_id: str | None = None,
        correlation_key: str | None = None,
        outcome: str,
        detail: str | None = None,
    ) -> bool:
        if not external_call_id and not correlation_key:
            return False
        statement = select(UrgentCallAttempt).with_for_update()
        if external_call_id:
            statement = statement.where(UrgentCallAttempt.external_call_id == external_call_id)
        else:
            statement = statement.where(UrgentCallAttempt.correlation_key == correlation_key)
        attempt = self.db.scalar(statement)
        if attempt is None:
            self.db.rollback()
            return False
        if attempt.status in {UrgentCallStatus.answered, UrgentCallStatus.not_picked}:
            self.db.rollback()
            return True
        delivery = self.db.get(UrgentReminderDelivery, attempt.delivery_id, with_for_update=True)
        occurrence = self.db.get(ReminderOccurrence, delivery.occurrence_id, with_for_update=True) if delivery else None
        reminder = self.db.get(Reminder, delivery.reminder_id, with_for_update=True) if delivery else None
        if not delivery or not occurrence or not reminder:
            self.db.rollback()
            return False
        now = datetime.now(timezone.utc)
        attempt.outcome = outcome
        attempt.ended_at = now
        if outcome == "answered":
            attempt.status = UrgentCallStatus.answered
            delivery.status = UrgentDeliveryStatus.delivered
            delivery.worker_id = None
            delivery.lease_expires_at = None
            if occurrence.status == OccurrenceStatus.pending and reminder.status == ReminderStatus.active:
                occurrence.status = OccurrenceStatus.notified
        elif outcome == "not_picked":
            attempt.status = UrgentCallStatus.not_picked
            delivery.worker_id = None
            delivery.lease_expires_at = None
            if attempt.attempt_number < 2:
                delivery.status = UrgentDeliveryStatus.retry_pending
                delivery.next_attempt_at = now + timedelta(seconds=self.retry_delay_seconds)
            else:
                self._queue_telegram_fallback(delivery, reminder, occurrence)
        else:
            attempt.status = UrgentCallStatus.failed
            attempt.last_error = (detail or outcome)[:2000]
            delivery.worker_id = None
            delivery.lease_expires_at = None
            if attempt.attempt_number < 2:
                delivery.status = UrgentDeliveryStatus.retry_pending
                delivery.next_attempt_at = now + timedelta(seconds=self.retry_delay_seconds)
                delivery.last_error = attempt.last_error
            else:
                delivery.status = UrgentDeliveryStatus.failed
                delivery.last_error = attempt.last_error
        self.db.commit()
        return True

    def find_attempt(self, *, external_call_id: str | None = None, correlation_key: str | None = None):
        if external_call_id:
            found = self.db.scalar(select(UrgentCallAttempt).where(UrgentCallAttempt.external_call_id == external_call_id))
            if found is not None:
                return found
        if correlation_key:
            return self.db.scalar(select(UrgentCallAttempt).where(UrgentCallAttempt.correlation_key == correlation_key))
        return None

    def _queue_telegram_fallback(
        self,
        delivery: UrgentReminderDelivery,
        reminder: Reminder,
        occurrence: ReminderOccurrence,
    ) -> None:
        if delivery.status == UrgentDeliveryStatus.telegram_queued:
            return
        identity = default_telegram_identity(self.db, reminder.user_id)
        if identity is None:
            delivery.status = UrgentDeliveryStatus.failed
            delivery.last_error = "Telegram identity is no longer linked"
            return
        NotificationService(self.db).schedule(
            reminder,
            occurrence,
            content=(
                f"Urgent reminder: {reminder.title}. I called you twice but you did not pick up. "
                "Please complete this task."
            ),
            identity=identity,
            # The fallback is addressed by this Telegram identity, not by the
            # reminder's optional generic destination (which may be Discord or
            # an invalidated legacy destination).
            destination_id=None,
            destination_snapshot=identity.external_destination_id,
            delivery_sequence=1,
        )
        delivery.status = UrgentDeliveryStatus.telegram_queued
        delivery.last_error = None
