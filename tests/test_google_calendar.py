from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet

from app.config import Settings
from app.integrations.google_calendar.crypto import TokenCipher
from app.integrations.google_calendar.service import GoogleCalendarService
from app.integrations.google_calendar.worker import GoogleCalendarWorker
from app.models import (
    GoogleCalendarConnection,
    GoogleCalendarConnectionStatus,
    GoogleCalendarEvent,
    OccurrenceStatus,
    ReminderChange,
    ReminderChangeStatus,
)
from app.reminders.service import ReminderService
from app.schemas import ReminderCreate, UserCreate
from app.users.service import UserService


def calendar_settings():
    return Settings(
        database_url="sqlite://",
        google_calendar_client_id="client-id",
        google_calendar_client_secret="client-secret",
        google_calendar_redirect_uri="https://example.test/callback",
        google_calendar_token_encryption_key=Fernet.generate_key().decode(),
    )


class FakeCalendarClient:
    def __init__(self):
        self.upserts = []
        self.deletes = []

    def exchange_code(self, code, verifier):
        assert code == "auth-code"
        assert verifier
        return {"refresh_token": "refresh-token"}

    def refresh_access_token(self, connection, refresh_token):
        assert refresh_token == "refresh-token"
        return "access-token", datetime.now(timezone.utc) + timedelta(hours=1)

    def upsert_event(self, calendar_id, event_id, body, access_token):
        self.upserts.append((calendar_id, event_id, body, access_token))

    def delete_event(self, calendar_id, event_id, access_token):
        self.deletes.append((calendar_id, event_id, access_token))


def test_oauth_connection_is_single_use_and_backfills_pending_reminders(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    settings = calendar_settings()
    client = FakeCalendarClient()
    service = GoogleCalendarService(db, settings=settings, client=client)

    url = service.begin_connection(user.id)
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["https://www.googleapis.com/auth/calendar.events"]
    assert query["code_challenge_method"] == ["S256"]
    db.commit()

    service.finish_connection(state=query["state"][0], code="auth-code")
    connection = db.query(GoogleCalendarConnection).one()
    assert connection.status == GoogleCalendarConnectionStatus.active
    assert TokenCipher(settings.google_calendar_token_encryption_key).decrypt(
        connection.encrypted_refresh_token
    ) == "refresh-token"
    assert db.query(ReminderChange).count() == 0


def test_worker_projects_active_occurrence_and_deletes_after_cancel(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    settings = calendar_settings()
    client = FakeCalendarClient()
    connection = GoogleCalendarConnection(
        user_id=user.id,
        calendar_id="primary",
        encrypted_refresh_token=TokenCipher(settings.google_calendar_token_encryption_key).encrypt(
            "refresh-token"
        ),
    )
    db.add(connection)
    db.commit()
    reminder = ReminderService(db).create(
        user.id,
        ReminderCreate(
            title="Pay bill",
            schedule=datetime.now(timezone.utc) + timedelta(days=1),
        ),
    )

    worker = GoogleCalendarWorker(db, settings=settings, client=client)
    assert worker.run_once() == 1
    assert len(client.upserts) == 1
    assert db.query(GoogleCalendarEvent).count() == 1
    assert db.query(ReminderChange).one().status == ReminderChangeStatus.completed

    occurrence = reminder.occurrences[0]
    occurrence.status = OccurrenceStatus.canceled
    db.add(
        ReminderChange(
            occurrence_id=occurrence.id,
            status=ReminderChangeStatus.pending,
            next_attempt_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    assert worker.run_once() == 1
    assert len(client.deletes) == 1
    assert db.query(GoogleCalendarEvent).count() == 0
