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
from app.integrations.discord.client import DiscordClient
from app.integrations.discord.errors import DiscordDeliveryError, DiscordNotConfigured, DiscordOAuthError
from app.models import (
    ChannelAccount,
    ChannelAccountState,
    ChannelIdentity,
    ChannelIdentityStatus,
    ChannelKind,
    DiscordLinkAttempt,
)


AUTHORIZATION_ENDPOINT = "https://discord.com/oauth2/authorize"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class DiscordLinkService:
    def __init__(
        self,
        db: Session,
        *,
        settings: Settings | None = None,
        client: DiscordClient | None = None,
    ):
        self.db = db
        self.settings = settings or get_settings()
        self.client = client

    def _require_settings(self) -> None:
        if not all(
            (
                self.settings.discord_client_id,
                self.settings.discord_client_secret,
                self.settings.discord_redirect_uri,
                self.settings.discord_bot_token,
            )
        ):
            raise DiscordNotConfigured("Discord integration is not configured")

    def _client(self) -> DiscordClient:
        self._require_settings()
        return self.client or DiscordClient(bot_token=self.settings.discord_bot_token or "")

    def begin(self, user_id: uuid.UUID) -> str:
        self._require_settings()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        self.db.add(
            DiscordLinkAttempt(
                user_id=user_id,
                state_hash=_hash(state),
                code_verifier=verifier,
                expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=self.settings.discord_link_ttl_seconds),
            )
        )
        self.db.flush()
        params = {
            "client_id": self.settings.discord_client_id,
            "redirect_uri": self.settings.discord_redirect_uri,
            "response_type": "code",
            "scope": "identify",
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    def _consume(self, state: str) -> DiscordLinkAttempt:
        if not state.strip():
            raise DiscordOAuthError("Discord OAuth state is required")
        attempt = self.db.scalar(
            select(DiscordLinkAttempt)
            .where(DiscordLinkAttempt.state_hash == _hash(state))
            .with_for_update()
        )
        now = datetime.now(timezone.utc)
        expires_at = attempt.expires_at if attempt is not None else None
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if attempt is None or attempt.consumed_at is not None or expires_at <= now:
            raise DiscordOAuthError("Discord link is invalid or expired")
        attempt.consumed_at = now
        return attempt

    def complete(self, *, state: str, code: str | None, error: str | None = None) -> ChannelAccount:
        if error:
            raise DiscordOAuthError("Discord authorization was canceled")
        if not code:
            raise DiscordOAuthError("Discord did not return an authorization code")
        self._require_settings()
        attempt = self._consume(state)
        # A callback retry cannot exchange or consume the same link state again.
        self.db.commit()
        client = self._client()
        token = client.exchange_code(
            client_id=self.settings.discord_client_id or "",
            client_secret=self.settings.discord_client_secret or "",
            redirect_uri=self.settings.discord_redirect_uri or "",
            code=code,
            code_verifier=attempt.code_verifier,
        )
        profile = client.current_user(token["access_token"])
        discord_user_id = str(profile.get("id") or "").strip()
        if not discord_user_id:
            raise DiscordOAuthError("Discord did not return a user identity")
        existing_user_identity = self.db.scalar(
            select(ChannelIdentity).where(
                ChannelIdentity.user_id == attempt.user_id,
                ChannelIdentity.channel == ChannelKind.discord,
                ChannelIdentity.status == ChannelIdentityStatus.linked,
                ChannelIdentity.is_default.is_(True),
            )
        )
        if (
            existing_user_identity is not None
            and existing_user_identity.external_principal_id != discord_user_id
        ):
            raise DiscordOAuthError("A different Discord account is already linked to this user")
        account = self.db.scalar(
            select(ChannelAccount).where(
                ChannelAccount.channel == ChannelKind.discord,
                ChannelAccount.external_user_id == discord_user_id,
            )
        )
        if account is not None and account.user_id != attempt.user_id:
            raise DiscordOAuthError("That Discord account is already linked to another user")
        try:
            channel_id = client.create_dm(discord_user_id)
        except DiscordDeliveryError as exc:
            raise DiscordOAuthError("Discord could not open a direct message channel") from exc
        if account is None:
            account = ChannelAccount(
                user_id=attempt.user_id,
                channel=ChannelKind.discord,
                external_user_id=discord_user_id,
                external_chat_id=channel_id,
                onboarding_state=ChannelAccountState.completed,
            )
            self.db.add(account)
            self.db.flush()
        else:
            account.external_chat_id = channel_id
            account.onboarding_state = ChannelAccountState.completed
        identity = self.db.scalar(
            select(ChannelIdentity).where(
                ChannelIdentity.channel == ChannelKind.discord,
                ChannelIdentity.external_principal_id == discord_user_id,
            )
        )
        if identity is None:
            identity = ChannelIdentity(
                user_id=attempt.user_id,
                channel=ChannelKind.discord,
                external_principal_id=discord_user_id,
                external_destination_id=account.external_chat_id,
                status=ChannelIdentityStatus.linked,
                is_default=True,
                verified_at=datetime.now(timezone.utc),
            )
            self.db.add(identity)
        else:
            identity.user_id = attempt.user_id
            identity.external_destination_id = account.external_chat_id
            identity.status = ChannelIdentityStatus.linked
            identity.is_default = True
            identity.verified_at = identity.verified_at or datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(account)
        return account
