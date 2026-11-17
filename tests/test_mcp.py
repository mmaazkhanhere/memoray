import base64
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from app.config import Settings
from app.mcp.auth.service import McpAuthService, OAuthError
from app.mcp.tools import execute
from app.models import ChannelIdentity, ChannelIdentityStatus, ChannelKind
from app.schemas import UserCreate
from app.users.service import UserService


def mcp_settings():
    return Settings(
        mcp_enabled=True,
        mcp_public_url="https://memoray.example",
        mcp_token_secret="test-mcp-secret",
    )


def test_mcp_oauth_links_telegram_user_and_requires_pkce(db):
    user = UserService(db).create(UserCreate(name="Ada", timezone="Asia/Karachi"))
    settings = mcp_settings()
    verifier = "a-long-test-verifier"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    service = McpAuthService(db, settings=settings)
    _, nonce = service.begin_authorization(
        client_id="client-1",
        redirect_uri="https://chatgpt.example/callback",
        state="state-1",
        code_challenge=challenge,
        code_challenge_method="S256",
        scope="memoray:reminders.read",
        resource="https://memoray.example/mcp",
    )
    db.commit()
    service.approve_nonce(nonce, user.id)
    db.commit()
    redirect = service.poll_redirect(nonce)
    db.commit()
    code = parse_qs(urlparse(redirect).query)["code"][0]
    tokens = service.exchange_code(
        code=code,
        client_id="client-1",
        redirect_uri="https://chatgpt.example/callback",
        code_verifier=verifier,
        resource="https://memoray.example/mcp",
    )
    db.commit()
    identity = service.verify_bearer(tokens["access_token"])
    assert identity.user_id == user.id
    assert identity.scope == frozenset({"memoray:reminders.read"})

    with pytest.raises(OAuthError):
        service.exchange_code(
            code=code,
            client_id="client-1",
            redirect_uri="https://chatgpt.example/callback",
            code_verifier=verifier,
            resource="https://memoray.example/mcp",
        )


def test_mcp_reminder_creation_uses_linked_telegram_destination(db):
    user = UserService(db).create(UserCreate(name="Ada", timezone="UTC"))
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
    identity = type(
        "Identity",
        (),
        {
            "user_id": user.id,
            "client_id": "client-1",
            "scope": frozenset({"memoray:reminders.write"}),
            "require": lambda self, scope: None,
        },
    )()
    date = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    result = execute(
        "create_reminder",
        {"title": "Call Ada", "date": date, "time": "10:00"},
        identity,
        db,
    )
    assert result["operation"] == "created"
    assert result["reminder"]["destination_id"]
