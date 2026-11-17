from __future__ import annotations

import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.integrations.google_calendar.client import GoogleCalendarClient
from app.integrations.google_calendar.crypto import TokenCipher
from app.integrations.google_calendar.errors import CalendarNotConfigured, OAuthStateError
from app.integrations.google_calendar.oauth import OAuthService, _require_settings
from app.models import (
    GoogleCalendarConnection,
    GoogleCalendarConnectionStatus,
    OccurrenceStatus,
    Reminder,
    ReminderOccurrence,
    ReminderStatus,
)
from app.reminders.changes import queue_occurrence_changes


class GoogleCalendarService:
    """Connection lifecycle and backfill; external API calls stay in the client/worker."""

    def __init__(
        self,
        db: Session,
        *,
        settings: Settings | None = None,
        client: GoogleCalendarClient | None = None,
    ):
        self.db = db
        self.settings = settings or get_settings()
        self.oauth = OAuthService(db, settings=self.settings)
        self.client = client or GoogleCalendarClient(settings=self.settings)

    def begin_connection(self, user_id: uuid.UUID) -> str:
        return self.oauth.begin(user_id)

    def finish_connection(
        self,
        *,
        state: str,
        code: str | None,
        error: str | None = None,
    ) -> GoogleCalendarConnection:
        if error:
            raise OAuthStateError("Google Calendar authorization was canceled")
        if not code:
            raise OAuthStateError("Google did not return an authorization code")
        _require_settings(self.settings)
        attempt = self.oauth.consume(state)
        # Consuming state before the provider call makes callback replay harmless.
        self.db.commit()
        token_payload = self.client.exchange_code(code, attempt.code_verifier)
        cipher = TokenCipher(self.settings.google_calendar_token_encryption_key or "")
        connection = self.db.scalar(
            select(GoogleCalendarConnection).where(
                GoogleCalendarConnection.user_id == attempt.user_id
            )
        )
        refresh_token = token_payload.get("refresh_token")
        if connection is None and not refresh_token:
            raise CalendarNotConfigured("Google did not grant offline Calendar access")
        if connection is None:
            connection = GoogleCalendarConnection(
                user_id=attempt.user_id,
                calendar_id="primary",
                encrypted_refresh_token=cipher.encrypt(refresh_token),
                status=GoogleCalendarConnectionStatus.active,
            )
            self.db.add(connection)
            self.db.flush()
        elif refresh_token:
            connection.encrypted_refresh_token = cipher.encrypt(refresh_token)
            connection.status = GoogleCalendarConnectionStatus.active
        else:
            connection.status = GoogleCalendarConnectionStatus.active

        # Backfill current state, including reminders created while disconnected.
        all_occurrence_ids = list(
            self.db.scalars(
                select(ReminderOccurrence.id)
                .join(Reminder, Reminder.id == ReminderOccurrence.reminder_id)
                .where(
                    Reminder.user_id == attempt.user_id,
                    Reminder.status == ReminderStatus.active,
                    ReminderOccurrence.status.in_(
                        [OccurrenceStatus.pending, OccurrenceStatus.notified]
                    ),
                )
            )
        )
        queue_occurrence_changes(self.db, all_occurrence_ids)
        self.db.commit()
        return connection

    def connection_for_user(self, user_id: uuid.UUID) -> GoogleCalendarConnection | None:
        return self.db.scalar(
            select(GoogleCalendarConnection).where(GoogleCalendarConnection.user_id == user_id)
        )

    def connect_command(self, user_id: uuid.UUID) -> str:
        try:
            url = self.begin_connection(user_id)
            return (
                "Open this link to connect your Google Calendar:\n"
                f"{url}\n\nYou can keep using reminders while this is optional."
            )
        except CalendarNotConfigured:
            return (
                "Google Calendar is not configured yet. "
                "You can continue using reminders normally."
            )
