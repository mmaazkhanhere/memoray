from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import McpAuthorizationCode, McpLinkAttempt, McpRefreshToken, User, UserStatus


READ_SCOPES = {
    "memoray:reminders.read",
    "memoray:memories.read",
}
WRITE_SCOPES = {
    "memoray:reminders.write",
    "memoray:memories.write",
}
ALL_SCOPES = READ_SCOPES | WRITE_SCOPES


class OAuthError(ValueError):
    def __init__(self, error: str, description: str, *, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


@dataclass(frozen=True)
class McpIdentity:
    user_id: uuid.UUID
    client_id: str
    scope: frozenset[str]
    resource: str

    def require(self, scope: str) -> None:
        if scope not in self.scope:
            raise OAuthError("insufficient_scope", f"scope {scope} is required", status=403)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _valid_redirect_uri(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" or (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
    )


def _resource(value: str) -> str:
    return value.rstrip("/")


class McpAuthService:
    def __init__(self, db: Session, *, settings: Settings | None = None):
        self.db = db
        self.settings = settings or get_settings()

    @property
    def issuer(self) -> str:
        return (self.settings.mcp_issuer or self.settings.mcp_public_url).rstrip("/")

    def begin_authorization(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
        scope: str,
        resource: str,
    ) -> tuple[McpLinkAttempt, str]:
        if not client_id.strip() or not state.strip():
            raise OAuthError("invalid_request", "client_id and state are required")
        if code_challenge_method != "S256" or not code_challenge:
            raise OAuthError("invalid_request", "S256 PKCE is required")
        if not _valid_redirect_uri(redirect_uri):
            raise OAuthError("invalid_request", "redirect_uri must use HTTPS")
        requested = set(scope.split()) if scope else set()
        if not requested or not requested.issubset(ALL_SCOPES):
            raise OAuthError("invalid_scope", "one or more requested scopes are unsupported")
        expected_resource = _resource(self.issuer + "/mcp")
        if resource and _resource(resource) != expected_resource:
            raise OAuthError("invalid_target", "resource is not this MCP server")
        nonce = secrets.token_urlsafe(32)
        attempt = McpLinkAttempt(
            nonce_hash=_hash(nonce),
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            scope=" ".join(sorted(requested)),
            resource=expected_resource,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=self.settings.mcp_link_ttl_seconds),
        )
        self.db.add(attempt)
        self.db.flush()
        return attempt, nonce

    def approve_nonce(self, nonce: str, user_id: uuid.UUID) -> None:
        attempt = self.db.scalar(
            select(McpLinkAttempt)
            .where(McpLinkAttempt.nonce_hash == _hash(nonce))
            .with_for_update()
        )
        now = datetime.now(timezone.utc)
        if attempt is None or _as_utc(attempt.expires_at) <= now or attempt.approved_at is not None:
            raise OAuthError("invalid_request", "link code is invalid or expired")
        user = self.db.get(User, user_id)
        if user is None or user.status != UserStatus.active:
            raise OAuthError("access_denied", "the Memoray account is not active")
        attempt.user_id = user_id
        attempt.approved_at = now
        self.db.flush()

    def poll_redirect(self, nonce: str) -> str | None:
        attempt = self.db.scalar(
            select(McpLinkAttempt)
            .where(McpLinkAttempt.nonce_hash == _hash(nonce))
            .with_for_update()
        )
        if attempt is None:
            raise OAuthError("invalid_request", "link code is invalid or expired")
        now = datetime.now(timezone.utc)
        if _as_utc(attempt.expires_at) <= now:
            raise OAuthError("invalid_request", "link code is invalid or expired")
        if attempt.user_id is None:
            return None
        if attempt.code_issued_at is not None:
            return None
        code = secrets.token_urlsafe(48)
        self.db.add(
            McpAuthorizationCode(
                code_hash=_hash(code),
                user_id=attempt.user_id,
                client_id=attempt.client_id,
                redirect_uri=attempt.redirect_uri,
                code_challenge=attempt.code_challenge,
                scope=attempt.scope,
                resource=attempt.resource,
                expires_at=now + timedelta(minutes=2),
            )
        )
        attempt.code_issued_at = now
        self.db.flush()
        return f"{attempt.redirect_uri}?{urlencode({'code': code, 'state': attempt.state})}"

    def exchange_code(
        self, *, code: str, client_id: str, redirect_uri: str, code_verifier: str, resource: str
    ) -> dict:
        record = self.db.scalar(
            select(McpAuthorizationCode)
            .where(McpAuthorizationCode.code_hash == _hash(code))
            .with_for_update()
        )
        now = datetime.now(timezone.utc)
        if (
            record is None
            or record.consumed_at is not None
            or _as_utc(record.expires_at) <= now
            or record.client_id != client_id
            or record.redirect_uri != redirect_uri
            or _resource(record.resource) != _resource(resource)
            or not hmac.compare_digest(record.code_challenge, _pkce_challenge(code_verifier))
        ):
            raise OAuthError("invalid_grant", "authorization code is invalid or expired")
        record.consumed_at = now
        self.db.flush()
        return self._issue_tokens(record.user_id, client_id, record.scope, record.resource)

    def refresh(self, *, refresh_token: str, client_id: str, resource: str) -> dict:
        record = self.db.scalar(
            select(McpRefreshToken)
            .where(McpRefreshToken.token_hash == _hash(refresh_token))
            .with_for_update()
        )
        now = datetime.now(timezone.utc)
        if (
            record is None
            or record.revoked_at is not None
            or _as_utc(record.expires_at) <= now
            or record.client_id != client_id
            or _resource(record.resource) != _resource(resource)
        ):
            raise OAuthError("invalid_grant", "refresh token is invalid or expired")
        record.revoked_at = now
        self.db.flush()
        return self._issue_tokens(record.user_id, client_id, record.scope, resource)

    def verify_bearer(self, token: str) -> McpIdentity:
        try:
            encoded, signature = token.split(".", 1)
            expected = _b64(
                hmac.new(self.settings.mcp_token_secret.encode(), encoded.encode(), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(_unb64(encoded))
            if int(payload["exp"]) <= int(datetime.now(timezone.utc).timestamp()):
                raise ValueError
            user_id = uuid.UUID(payload["sub"])
            user = self.db.get(User, user_id)
            if user is None or user.status != UserStatus.active:
                raise ValueError
            expected_resource = _resource(self.issuer + "/mcp")
            if _resource(payload["aud"]) != expected_resource:
                raise ValueError
            return McpIdentity(
                user_id=user_id,
                client_id=payload["client_id"],
                scope=frozenset(payload["scope"].split()),
                resource=payload["aud"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            raise OAuthError("invalid_token", "access token is invalid or expired", status=401)

    def _issue_tokens(self, user_id: uuid.UUID, client_id: str, scope: str, resource: str) -> dict:
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user_id),
            "client_id": client_id,
            "scope": scope,
            "aud": resource,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self.settings.mcp_access_token_ttl_seconds)).timestamp()),
            "jti": secrets.token_urlsafe(16),
        }
        encoded = _b64(json.dumps(payload, separators=(",", ":")).encode())
        signature = _b64(
            hmac.new(self.settings.mcp_token_secret.encode(), encoded.encode(), hashlib.sha256).digest()
        )
        refresh = secrets.token_urlsafe(48)
        self.db.add(
            McpRefreshToken(
                token_hash=_hash(refresh),
                user_id=user_id,
                client_id=client_id,
                scope=scope,
                resource=resource,
                expires_at=now + timedelta(seconds=self.settings.mcp_refresh_token_ttl_seconds),
            )
        )
        return {
            "access_token": f"{encoded}.{signature}",
            "token_type": "Bearer",
            "expires_in": self.settings.mcp_access_token_ttl_seconds,
            "refresh_token": refresh,
            "scope": scope,
        }
