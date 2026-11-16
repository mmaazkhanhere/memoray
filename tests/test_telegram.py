import uuid

import pytest

from app.channels.contracts import IncomingContact, IncomingMessage
from app.channels.service import ChannelMessageService
from app.integrations.telegram.adapter import TelegramUpdateError, parse_update
from app.models import (
    ChannelAccount,
    ChannelAccountState,
    InboundEvent,
    OutboundMessage,
    PhoneLink,
    User,
)


class FakeAgent:
    def handle(self, user_id, message, conversation_id):
        return type(
            "Result",
            (),
            {"conversation_id": conversation_id or uuid.uuid4(), "response": "agent reply"},
        )()


def channel_service(db):
    return ChannelMessageService(db, lambda _db: FakeAgent())


def incoming(update_id, sender="10", text=None, *, display_name=None, contact=None, chat=None):
    return IncomingMessage(
        channel="telegram",
        external_update_id=str(update_id),
        external_user_id=sender,
        external_chat_id=chat or sender,
        text=text,
        display_name=display_name,
        contact=contact,
    )


def test_first_start_creates_one_account_and_repeated_start_reuses_it(db):
    service = channel_service(db)
    first = service.process(incoming(1, text="/start", display_name="Ada Lovelace"))
    second = service.process(incoming(2, text="/start", display_name="Ada Lovelace"))

    assert "timezone" in first[0].content.lower()
    assert "timezone" in second[0].content.lower()
    assert db.query(User).count() == 1
    assert db.query(ChannelAccount).count() == 1


def test_onboarding_rejects_invalid_timezone_and_can_skip_phone(db):
    service = channel_service(db)
    service.process(incoming(1, text="/start", display_name="Ada"))
    invalid = service.process(incoming(2, text="Not/A_Timezone"))
    service.process(incoming(3, text="Asia/Karachi"))
    skipped = service.process(incoming(4, text="Skip for now"))

    assert "invalid" in invalid[-1].content.lower()
    assert "live calls" in skipped[-1].content.lower()
    account = db.query(ChannelAccount).one()
    assert account.onboarding_state == ChannelAccountState.completed
    assert db.query(User).one().timezone == "Asia/Karachi"


def test_contact_must_belong_to_sender_and_valid_contact_is_normalized(db):
    service = channel_service(db)
    service.process(incoming(1, text="/start", display_name="Ada"))
    service.process(incoming(2, text="UTC"))
    rejected = service.process(
        incoming(3, contact=IncomingContact("+14155550123", "other"))
    )
    linked = service.process(
        incoming(4, contact=IncomingContact("+1 (415) 555-0123", "10"))
    )

    assert "another sender" in rejected[-1].content
    assert "linked" in linked[-1].content
    assert db.query(PhoneLink).one().e164_number == "+14155550123"


def test_active_phone_is_unique_across_accounts(db):
    service = channel_service(db)
    for update_id, sender in ((1, "10"), (10, "20")):
        service.process(incoming(update_id, sender=sender, text="/start", display_name=sender))
        service.process(incoming(update_id + 1, sender=sender, text="UTC"))
    service.process(incoming(3, sender="10", contact=IncomingContact("+14155550123", "10")))
    rejected = service.process(
        incoming(12, sender="20", contact=IncomingContact("+14155550123", "20"))
    )

    assert "already linked" in rejected[-1].content
    assert db.query(PhoneLink).count() == 1


def test_duplicate_update_is_not_processed_twice_and_senders_are_isolated(db):
    service = channel_service(db)
    service.process(incoming(1, text="/start", display_name="A", sender="10"))
    duplicate = service.process(incoming(1, text="/start", display_name="A", sender="10"))
    service.process(incoming(2, text="/start", display_name="B", sender="20"))

    assert db.query(InboundEvent).count() == 2
    assert db.query(User).count() == 2
    assert db.query(ChannelAccount).count() == 2
    # The duplicate only exposes already-persisted pending replies; it creates none.
    assert db.query(OutboundMessage).count() == 2
    assert duplicate


def test_telegram_parser_requires_sender_and_preserves_contact_owner():
    parsed = parse_update(
        {
            "update_id": 42,
            "message": {
                "from": {"id": 7, "first_name": "Ada", "last_name": "Lovelace"},
                "chat": {"id": 8},
                "contact": {"phone_number": "+14155550123", "user_id": 7},
            },
        }
    )
    assert parsed.external_user_id == "7"
    assert parsed.display_name == "Ada Lovelace"
    assert parsed.contact.owner_external_id == "7"

    with pytest.raises(TelegramUpdateError):
        parse_update({"update_id": 42})


def test_database_failure_does_not_report_onboarding_success(db, monkeypatch):
    service = channel_service(db)
    original_commit = db.commit

    def fail_commit():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        service.process(incoming(1, text="/start", display_name="Ada"))
    monkeypatch.setattr(db, "commit", original_commit)
