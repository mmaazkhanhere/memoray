from urllib.parse import parse_qs, urlparse

from app.channels.contracts import IncomingMessage
from app.channels.service import ChannelMessageService
from app.config import Settings
from app.integrations.discord.linking import DiscordLinkService
from app.models import (
    ChannelAccount,
    ChannelIdentity,
    ChannelKind,
    User,
    UserStatus,
)


class FakeDiscordClient:
    def __init__(self, user_id="discord-1"):
        self.user_id = user_id
        self.created_dms = []

    def exchange_code(self, **kwargs):
        assert kwargs["code"] == "code-1"
        return {"access_token": "access-1"}

    def current_user(self, access_token):
        assert access_token == "access-1"
        return {"id": self.user_id, "username": "Ada"}

    def create_dm(self, recipient_id):
        self.created_dms.append(recipient_id)
        return "dm-1"


def discord_settings():
    return Settings(
        discord_bot_token="bot-token",
        discord_client_id="client-id",
        discord_client_secret="client-secret",
        discord_redirect_uri="https://example.test/integrations/discord/oauth/callback",
    )


def test_discord_link_is_single_use_and_creates_identity(db):
    user = User(name="Ada", timezone="UTC", status=UserStatus.active)
    db.add(user)
    db.commit()

    client = FakeDiscordClient()
    service = DiscordLinkService(db, settings=discord_settings(), client=client)
    url = service.begin(user.id)
    state = parse_qs(urlparse(url).query)["state"][0]
    account = service.complete(state=state, code="code-1")

    assert account.user_id == user.id
    assert account.channel == ChannelKind.discord
    assert account.external_chat_id == "dm-1"
    assert db.query(ChannelIdentity).one().external_principal_id == "discord-1"
    assert client.created_dms == ["discord-1"]

    try:
        service.complete(state=state, code="code-1")
    except Exception as exc:
        assert "invalid or expired" in str(exc)
    else:
        raise AssertionError("a Discord link state must not be reusable")


def test_unlinked_discord_message_does_not_create_memoray_user(db):
    message = IncomingMessage(
        channel="discord",
        external_update_id="message-1",
        external_user_id="discord-unknown",
        external_chat_id="dm-unknown",
        text="remember that I like tea",
    )

    records = ChannelMessageService(db, lambda _db: object()).process(
        message, create_account=False
    )

    assert len(records) == 1
    assert "connect_discord" in records[0].content
    assert db.query(User).count() == 0
    assert db.query(ChannelAccount).count() == 0
