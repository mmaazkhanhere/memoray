from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    ChannelIdentity,
    ChannelIdentityStatus,
    ChannelKind,
    NotificationDelivery,
    OccurrenceStatus,
    PhoneLink,
    User,
    UserStatus,
    UrgentDeliveryStatus,
)
from app.reminders.service import ReminderService
from app.schemas import ReminderCreate, UserCreate
from app.schemas import ReminderRescheduleRequest, RescheduleScope
from app.reminders.rescheduling import ReminderRescheduler
from app.reminders.snoozing import ReminderSnoozer
from app.schemas import SnoozeRequest
from app.urgent_reminders.service import UrgentReminderService
from app.users.service import UserService


def linked_user(db):
    user = UserService(db).create(UserCreate(name="Caller", timezone="UTC"))
    db.add(PhoneLink(user_id=user.id, e164_number="+14155550123", is_active=True))
    db.add(
        ChannelIdentity(
            user_id=user.id,
            channel=ChannelKind.telegram,
            external_principal_id="telegram-user",
            external_destination_id="telegram-chat",
            status=ChannelIdentityStatus.linked,
            is_default=True,
        )
    )
    db.commit()
    return user


def make_urgent(db):
    user = linked_user(db)
    reminder = ReminderService(db).create(
        user.id,
        ReminderCreate(
            title="Submit report",
            schedule=datetime.now(timezone.utc) + timedelta(hours=1),
            is_urgent=True,
        ),
    )
    occurrence = reminder.occurrences[0]
    occurrence.next_due_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    occurrence.urgent_delivery.next_attempt_at = occurrence.next_due_at
    db.commit()
    return user, reminder, occurrence


def test_urgent_reminder_requires_phone_and_telegram(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    with pytest.raises(ValueError, match="phone"):
        ReminderService(db).create(
            user.id,
            ReminderCreate(
                title="Pay bill",
                schedule=datetime.now(timezone.utc) + timedelta(hours=1),
                is_urgent=True,
            ),
        )

    phone_user = UserService(db).create(UserCreate(name="B", timezone="UTC"))
    db.add(PhoneLink(user_id=phone_user.id, e164_number="+14155550124", is_active=True))
    db.commit()
    with pytest.raises(ValueError, match="Telegram"):
        ReminderService(db).create(
            phone_user.id,
            ReminderCreate(
                title="Pay bill",
                schedule=datetime.now(timezone.utc) + timedelta(hours=1),
                is_urgent=True,
            ),
        )
    assert db.query(User).filter(User.id == phone_user.id).one()


def test_urgent_call_is_delivered_when_human_answers(db):
    _user, _reminder, occurrence = make_urgent(db)
    service = UrgentReminderService(db)
    claimed = service.claim_due("urgent-worker")
    assert len(claimed) == 1
    _delivery, attempt = claimed[0]
    assert service.record_call_started(attempt.correlation_key, "vapi-call-1")
    assert service.handle_provider_outcome(external_call_id="vapi-call-1", outcome="answered")
    db.refresh(occurrence)
    assert occurrence.status == OccurrenceStatus.notified


def test_two_unanswered_calls_queue_one_telegram_escalation(db):
    _user, _reminder, occurrence = make_urgent(db)
    service = UrgentReminderService(db)
    first = service.claim_due("urgent-worker")[0][1]
    service.record_call_started(first.correlation_key, "vapi-call-1")
    service.handle_provider_outcome(external_call_id="vapi-call-1", outcome="not_picked")

    delivery = service.find_attempt(correlation_key=first.correlation_key).delivery
    delivery.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    second = service.claim_due("urgent-worker")[0][1]
    service.record_call_started(second.correlation_key, "vapi-call-2")
    service.handle_provider_outcome(external_call_id="vapi-call-2", outcome="not_picked")

    db.refresh(delivery)
    assert delivery.status == UrgentDeliveryStatus.telegram_queued
    notification = db.query(NotificationDelivery).one()
    assert notification.destination_snapshot == "telegram-chat"
    assert "did not pick up" in notification.content
    db.refresh(occurrence)
    assert occurrence.status == OccurrenceStatus.pending


def test_replayed_answer_callback_is_idempotent(db):
    _user, _reminder, occurrence = make_urgent(db)
    service = UrgentReminderService(db)
    attempt = service.claim_due("urgent-worker")[0][1]
    service.record_call_started(attempt.correlation_key, "vapi-call-1")
    assert service.handle_provider_outcome(external_call_id="vapi-call-1", outcome="answered")
    assert service.handle_provider_outcome(external_call_id="vapi-call-1", outcome="answered")
    assert db.query(NotificationDelivery).count() == 0
    db.refresh(occurrence)
    assert occurrence.status == OccurrenceStatus.notified


def test_rescheduling_urgent_occurrence_resets_call_budget(db):
    user, reminder, occurrence = make_urgent(db)
    service = UrgentReminderService(db)
    attempt = service.claim_due("urgent-worker")[0][1]
    service.record_call_started(attempt.correlation_key, "vapi-call-1")
    service.handle_provider_outcome(external_call_id="vapi-call-1", outcome="not_picked")
    next_time = datetime.now(timezone.utc) + timedelta(hours=2)
    updated = ReminderRescheduler(db).reschedule(
        user.id,
        reminder.id,
        ReminderRescheduleRequest(schedule=next_time, scope=RescheduleScope.series),
    )
    db.refresh(updated.occurrences[0].urgent_delivery)
    assert updated.occurrences[0].urgent_delivery.attempt_count == 0
    assert updated.occurrences[0].urgent_delivery.status == UrgentDeliveryStatus.pending


def test_snoozing_answered_urgent_occurrence_starts_new_call_workflow(db):
    user, reminder, occurrence = make_urgent(db)
    service = UrgentReminderService(db)
    attempt = service.claim_due("urgent-worker")[0][1]
    service.record_call_started(attempt.correlation_key, "vapi-call-1")
    service.handle_provider_outcome(external_call_id="vapi-call-1", outcome="answered")
    updated = ReminderSnoozer(db).snooze(user.id, reminder.id, occurrence.id, SnoozeRequest(duration_seconds=60))
    db.refresh(updated.occurrences[0].urgent_delivery)
    assert updated.occurrences[0].urgent_delivery.status == UrgentDeliveryStatus.pending
    assert updated.occurrences[0].urgent_delivery.attempt_count == 0


def test_provider_dispatch_failure_retries_same_user_attempt_key(db):
    _user, _reminder, _occurrence = make_urgent(db)
    service = UrgentReminderService(db)
    first = service.claim_due("urgent-worker")[0][1]
    assert service.record_dispatch_failure(first.correlation_key, "temporary provider error")
    delivery = service.find_attempt(correlation_key=first.correlation_key)
    assert delivery is None
    row = db.query(type(_occurrence.urgent_delivery)).one()
    row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    second = service.claim_due("urgent-worker")[0][1]
    assert second.attempt_number == 1
    assert second.correlation_key == first.correlation_key
