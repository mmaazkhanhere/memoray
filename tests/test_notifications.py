from datetime import datetime, timedelta, timezone

from app.models import (
    DeliveryChannel,
    DeliveryDestination,
    NotificationStatus,
    OccurrenceStatus,
    Reminder,
    ReminderOccurrence,
    ReminderStatus,
    User,
    UserStatus,
)
from app.notifications.service import NotificationService


def make_notification(db, *, due=True):
    user = User(name="A", timezone="UTC", status=UserStatus.active)
    db.add(user)
    destination = DeliveryDestination(
        user=user,
        channel=DeliveryChannel.in_app,
        identity="user:test",
        is_linked=True,
        is_valid=True,
    )
    reminder = Reminder(
        user=user,
        title="Pay bill",
        normalized_title="pay bill",
        schedule=datetime.now(timezone.utc) + timedelta(hours=1),
        destination=destination,
        status=ReminderStatus.active,
    )
    occurrence = ReminderOccurrence(
        reminder=reminder,
        schedule=reminder.schedule,
        next_due_at=(
            datetime.now(timezone.utc) - timedelta(seconds=1)
            if due
            else reminder.schedule
        ),
        status=OccurrenceStatus.pending,
    )
    db.add(reminder)
    db.flush()
    row = NotificationService(db).schedule(
        reminder,
        occurrence,
        channel=DeliveryChannel.in_app,
        destination_id=destination.id,
        destination_snapshot=destination.identity,
        next_attempt_at=occurrence.next_due_at,
    )
    db.commit()
    return row


def test_claim_is_exclusive_and_success_notifies_occurrence(db):
    row = make_notification(db)
    service = NotificationService(db)

    claimed = service.claim_due("worker-a")
    assert [item.id for item in claimed] == [row.id]
    assert service.claim_due("worker-b") == []

    assert service.finalize(row.id, "worker-a", classification="success")
    db.refresh(row)
    assert row.status == NotificationStatus.sent
    assert row.occurrence.status == OccurrenceStatus.notified


def test_ineligible_occurrence_is_not_sent(db):
    row = make_notification(db)
    row.occurrence.status = OccurrenceStatus.canceled
    db.commit()

    assert NotificationService(db).claim_due("worker-a") == []
    db.refresh(row)
    assert row.status == NotificationStatus.pending


def test_permanent_and_unknown_results_are_explicit(db):
    permanent = make_notification(db)
    service = NotificationService(db)
    service.claim_due("worker-a")
    assert service.finalize(permanent.id, "worker-a", classification="permanent", error="chat not found")
    db.refresh(permanent)
    assert permanent.status == NotificationStatus.failed
    assert permanent.last_error == "chat not found"

    unknown = make_notification(db)
    service.claim_due("worker-b")
    assert service.finalize(unknown.id, "worker-b", classification="unknown", error="timeout")
    db.refresh(unknown)
    assert unknown.status == NotificationStatus.pending
    unknown.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    service.claim_due("worker-b")
    assert service.finalize(unknown.id, "worker-b", classification="unknown", error="timeout")
    db.refresh(unknown)
    assert unknown.status == NotificationStatus.pending
    unknown.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    service.claim_due("worker-b")
    assert service.finalize(unknown.id, "worker-b", classification="unknown", error="timeout")
    db.refresh(unknown)
    assert unknown.status == NotificationStatus.unknown
