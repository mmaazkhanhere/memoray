"""Durable, integration-neutral reminder change notifications."""

from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ReminderChange, ReminderChangeStatus


def queue_occurrence_change(db: Session, occurrence_id: uuid.UUID) -> ReminderChange:
    """Make an occurrence eligible for external projection without calling an adapter."""
    now = datetime.now(timezone.utc)
    existing = db.scalar(
        select(ReminderChange)
        .where(
            ReminderChange.occurrence_id == occurrence_id,
            ReminderChange.status.in_([ReminderChangeStatus.pending, ReminderChangeStatus.sending]),
        )
        .order_by(ReminderChange.created_at.desc())
    )
    if existing is not None:
        existing.status = ReminderChangeStatus.pending
        existing.next_attempt_at = now
        existing.lease_expires_at = None
        existing.worker_id = None
        existing.last_error = None
        return existing
    change = ReminderChange(
        occurrence_id=occurrence_id,
        status=ReminderChangeStatus.pending,
        next_attempt_at=now,
    )
    db.add(change)
    return change


def queue_occurrence_changes(db: Session, occurrence_ids: list[uuid.UUID]) -> None:
    for occurrence_id in dict.fromkeys(occurrence_ids):
        queue_occurrence_change(db, occurrence_id)
