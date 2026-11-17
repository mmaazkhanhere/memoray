from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.integrations.google_calendar.client import GoogleCalendarClient
from app.integrations.google_calendar.crypto import TokenCipher
from app.integrations.google_calendar.errors import GoogleCalendarApiError
from app.integrations.google_calendar.projection import event_body, external_event_id, should_exist
from app.models import (
    GoogleCalendarConnection,
    GoogleCalendarConnectionStatus,
    GoogleCalendarEvent,
    Reminder,
    ReminderOccurrence,
    ReminderChange,
    ReminderChangeStatus,
)


class GoogleCalendarWorker:
    def __init__(
        self,
        db: Session,
        *,
        settings: Settings | None = None,
        client: GoogleCalendarClient | None = None,
        worker_id: str | None = None,
    ):
        self.db = db
        self.settings = settings or get_settings()
        self.client = client or GoogleCalendarClient(settings=self.settings)
        self.worker_id = worker_id or self.settings.google_calendar_worker_id

    def run_once(self, limit: int = 20) -> int:
        processed = 0
        for _ in range(limit):
            change = self._claim()
            if change is None:
                break
            self._process(change)
            processed += 1
        return processed

    def _claim(self) -> ReminderChange | None:
        now = datetime.now(timezone.utc)
        change = self.db.scalar(
            select(ReminderChange)
            .where(
                or_(
                    ReminderChange.status == ReminderChangeStatus.pending,
                    (
                        (ReminderChange.status == ReminderChangeStatus.sending)
                        & (ReminderChange.lease_expires_at < now)
                    ),
                ),
                ReminderChange.next_attempt_at <= now,
            )
            .order_by(ReminderChange.next_attempt_at.asc(), ReminderChange.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if change is None:
            return None
        change.status = ReminderChangeStatus.sending
        change.worker_id = self.worker_id
        change.lease_expires_at = now + timedelta(
            seconds=self.settings.google_calendar_job_lease_seconds
        )
        change.attempt_count += 1
        self.db.commit()
        return change

    def _process(self, change: ReminderChange) -> None:
        try:
            self._synchronize(change.occurrence_id)
            change = self.db.get(ReminderChange, change.id)
            if change is not None:
                change.status = ReminderChangeStatus.completed
                change.lease_expires_at = None
                change.worker_id = None
                change.last_error = None
                self.db.commit()
        except GoogleCalendarApiError as exc:
            self._failure(change.id, exc)
        except Exception as exc:  # pragma: no cover - defensive worker isolation
            self._failure(change.id, exc, retryable=True)

    def _synchronize(self, occurrence_id: uuid.UUID) -> None:
        occurrence = self.db.scalar(
            select(ReminderOccurrence)
            .options(selectinload(ReminderOccurrence.reminder).selectinload(Reminder.user))
            .where(ReminderOccurrence.id == occurrence_id)
        )
        if occurrence is None:
            return
        connection = self.db.scalar(
            select(GoogleCalendarConnection).where(
                GoogleCalendarConnection.user_id == occurrence.reminder.user_id,
                GoogleCalendarConnection.status == GoogleCalendarConnectionStatus.active,
            )
        )
        if connection is None:
            return
        cipher = TokenCipher(self.settings.google_calendar_token_encryption_key or "")
        refresh_token = cipher.decrypt(connection.encrypted_refresh_token)
        access_token, expires_at = self.client.refresh_access_token(connection, refresh_token)
        connection.token_expires_at = expires_at
        event_id = external_event_id(occurrence.id)
        mapping = self.db.scalar(
            select(GoogleCalendarEvent).where(
                GoogleCalendarEvent.connection_id == connection.id,
                GoogleCalendarEvent.occurrence_id == occurrence.id,
            )
        )
        if should_exist(occurrence):
            if mapping is None:
                mapping = GoogleCalendarEvent(
                    connection_id=connection.id,
                    occurrence_id=occurrence.id,
                    external_event_id=event_id,
                )
                self.db.add(mapping)
                self.db.flush()
            self.client.upsert_event(
                connection.calendar_id,
                mapping.external_event_id,
                event_body(
                    occurrence,
                    timezone_name=occurrence.reminder.user.timezone,
                    settings=self.settings,
                ),
                access_token,
            )
        elif mapping is not None:
            self.client.delete_event(
                connection.calendar_id, mapping.external_event_id, access_token
            )
            self.db.delete(mapping)
        if mapping is not None:
            mapping.last_synced_at = datetime.now(timezone.utc)
            mapping.last_error = None

    def _failure(
        self, change_id: uuid.UUID, error: Exception, *, retryable: bool | None = None
    ) -> None:
        change = self.db.get(ReminderChange, change_id)
        if change is None:
            return
        is_retryable = retryable if retryable is not None else getattr(error, "retryable", False)
        if isinstance(error, GoogleCalendarApiError) and error.status_code in {401, 403}:
            # Marking the connection requires reauthorization prevents an endless retry loop.
            connection = self.db.scalar(
                select(GoogleCalendarConnection)
                .join(
                    GoogleCalendarEvent,
                    GoogleCalendarEvent.connection_id == GoogleCalendarConnection.id,
                )
                .join(
                    ReminderChange,
                    ReminderChange.occurrence_id == GoogleCalendarEvent.occurrence_id,
                )
                .where(ReminderChange.id == change_id)
            )
            if connection is not None:
                connection.status = GoogleCalendarConnectionStatus.reauthorization_required
            is_retryable = False
        change.status = (
            ReminderChangeStatus.pending if is_retryable else ReminderChangeStatus.failed
        )
        change.lease_expires_at = None
        change.worker_id = None
        change.last_error = str(error)[:1000]
        if is_retryable:
            delay = min(3600, 2 ** min(change.attempt_count, 10))
            change.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        self.db.commit()
