from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.integrations.google_calendar.errors import CalendarNotConfigured, OAuthStateError
from app.models import GoogleCalendarOAuthAttempt


AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def _require_settings(settings: Settings) -> None:
    if not all(
        (
            settings.google_calendar_client_id,
            settings.google_calendar_client_secret,
            settings.google_calendar_redirect_uri,
            settings.google_calendar_token_encryption_key,
        )
    ):
        raise CalendarNotConfigured("Google Calendar is not configured")
    try:
        from app.integrations.google_calendar.crypto import TokenCipher

        TokenCipher(settings.google_calendar_token_encryption_key or "")
    except (RuntimeError, ValueError) as exc:
        raise CalendarNotConfigured(
            "Google Calendar token encryption is not configured correctly"
        ) from exc


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class OAuthService:
    def __init__(self, db: Session, *, settings: Settings | None = None):
        self.db = db
        self.settings = settings or get_settings()

    def begin(self, user_id: uuid.UUID) -> str:
        _require_settings(self.settings)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        attempt = GoogleCalendarOAuthAttempt(
            user_id=user_id,
            state_hash=_state_hash(state),
            code_verifier=verifier,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=self.settings.google_calendar_oauth_state_ttl_seconds),
        )
        self.db.add(attempt)
        self.db.flush()
        params = {
            "client_id": self.settings.google_calendar_client_id,
            "redirect_uri": self.settings.google_calendar_redirect_uri,
            "response_type": "code",
            "scope": CALENDAR_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    def consume(self, state: str) -> GoogleCalendarOAuthAttempt:
        if not state.strip():
            raise OAuthStateError("OAuth state is required")
        attempt = self.db.scalar(
            select(GoogleCalendarOAuthAttempt)
            .where(GoogleCalendarOAuthAttempt.state_hash == _state_hash(state))
            .with_for_update()
        )
        now = datetime.now(timezone.utc)
        expires_at = attempt.expires_at if attempt is not None else None
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if attempt is None or attempt.consumed_at is not None or expires_at <= now:
            raise OAuthStateError("OAuth state is invalid or expired")
        attempt.consumed_at = now
        return attempt
