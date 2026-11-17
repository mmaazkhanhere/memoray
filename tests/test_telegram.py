import uuid

import pytest

from app.channels.contracts import IncomingContact, IncomingMessage, IncomingVoice
from app.channels.service import ChannelMessageService
from app.integrations.telegram.adapter import TelegramUpdateError, parse_update
from app.notifications.worker import TelegramDeliveryAdapter
from app.integrations.telegram.voice import (
    TelegramVoiceTranscriber,
    VoiceLimits,
    VoiceProcessingError,
)
from app.stt.protocol import AudioInput
from app.models import (
    ChannelAccount,
    ChannelAccountState,
    InboundEvent,
    OutboundMessage,
    PhoneLink,
    User,
    DeliveryChannel,
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


def test_skipped_phone_can_be_linked_later_with_command_and_contact(db):
    service = channel_service(db)
    service.process(incoming(1, text="/start", display_name="Ada"))
    service.process(incoming(2, text="UTC"))
    service.process(incoming(3, text="Skip for now"))

    prompt = service.process(incoming(4, text="/link phone"))
    assert "share your telegram contact" in prompt[-1].content.lower()
    assert prompt[-1].request_phone is True
    assert db.query(PhoneLink).count() == 0

    linked = service.process(
        incoming(5, contact=IncomingContact("+1 (415) 555-0123", "10"))
    )
    assert "phone is linked" in linked[-1].content.lower()
    assert db.query(PhoneLink).one().e164_number == "+14155550123"
    assert db.query(ChannelAccount).one().onboarding_state == ChannelAccountState.completed


def test_skipped_phone_can_be_linked_later_by_sharing_contact_directly(db):
    service = channel_service(db)
    service.process(incoming(1, text="/start", display_name="Ada"))
    service.process(incoming(2, text="UTC"))
    service.process(incoming(3, text="skip"))

    linked = service.process(
        incoming(4, contact=IncomingContact("+1 (415) 555-0123", "10"))
    )
    assert "phone is linked" in linked[-1].content.lower()
    assert db.query(PhoneLink).one().e164_number == "+14155550123"


def test_later_phone_link_prompt_can_be_canceled(db):
    service = channel_service(db)
    service.process(incoming(1, text="/start", display_name="Ada"))
    service.process(incoming(2, text="UTC"))
    service.process(incoming(3, text="skip"))
    service.process(incoming(4, text="/link phone"))

    canceled = service.process(incoming(5, text="/cancel"))
    assert "remains optional" in canceled[-1].content.lower()
    assert db.query(PhoneLink).count() == 0
    assert db.query(ChannelAccount).one().onboarding_state == ChannelAccountState.completed


def test_phone_link_accepts_typed_international_number_after_prompt(db):
    service = channel_service(db)
    service.process(incoming(1, text="/start", display_name="Ada"))
    service.process(incoming(2, text="UTC"))
    service.process(incoming(3, text="skip"))
    service.process(incoming(4, text="/link phone"))

    linked = service.process(incoming(5, text="+14155550123"))
    assert "phone is linked" in linked[-1].content.lower()
    assert db.query(PhoneLink).one().e164_number == "+14155550123"


def test_invalid_contact_keeps_contact_keyboard_and_explains_format(db):
    service = channel_service(db)
    service.process(incoming(1, text="/start", display_name="Ada"))
    service.process(incoming(2, text="UTC"))
    service.process(incoming(3, text="skip"))
    service.process(incoming(4, text="/link phone"))

    rejected = service.process(
        incoming(5, contact=IncomingContact("03001234567", "10"))
    )
    assert "international" in rejected[-1].content.lower()
    assert rejected[-1].request_phone is True
    assert db.query(PhoneLink).count() == 0


def test_telegram_contact_without_plus_can_be_normalized_as_international(db):
    service = channel_service(db)
    service.process(incoming(1, text="/start", display_name="Ada"))
    service.process(incoming(2, text="UTC"))
    service.process(incoming(3, text="skip"))

    linked = service.process(
        incoming(4, contact=IncomingContact("923001234567", "10"))
    )
    assert "phone is linked" in linked[-1].content.lower()
    assert db.query(PhoneLink).one().e164_number == "+923001234567"


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


def test_telegram_parser_extracts_done_callback():
    occurrence_id = uuid.uuid4()
    parsed = parse_update(
        {
            "update_id": 45,
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 7},
                "message": {"chat": {"id": 8}, "message_id": 99},
                "data": f"done:{occurrence_id}",
            },
        }
    )

    assert parsed.completion_occurrence_id == occurrence_id
    assert parsed.callback_query_id == "callback-1"
    assert parsed.callback_message_id == 99


def test_telegram_delivery_adapter_always_attaches_done_occurrence():
    class FakeClient:
        def __init__(self):
            self.kwargs = None

        def send_message(self, *args, **kwargs):
            self.kwargs = kwargs

    client = FakeClient()
    adapter = TelegramDeliveryAdapter.__new__(TelegramDeliveryAdapter)
    adapter.client = client
    occurrence_id = uuid.uuid4()
    notification = type(
        "Notification",
        (),
        {
            "destination_snapshot": "chat-1",
            "content": "Reminder: pay bill",
            "request_phone": False,
            "provider_idempotency_key": "key-1",
            "occurrence_id": occurrence_id,
            "channel": DeliveryChannel.telegram,
        },
    )()

    adapter.send(notification)

    assert client.kwargs["done_occurrence_id"] == occurrence_id


def test_telegram_parser_extracts_voice_metadata_and_rejects_invalid_voice():
    parsed = parse_update(
        {
            "update_id": 43,
            "message": {
                "from": {"id": 7},
                "chat": {"id": 8},
                "voice": {
                    "file_id": "file-1",
                    "file_unique_id": "unique-1",
                    "duration": 4,
                    "mime_type": "audio/ogg",
                    "file_size": 100,
                },
            },
        }
    )
    assert parsed.text is None
    assert parsed.voice.file_id == "file-1"
    assert parsed.voice.mime_type == "audio/ogg"

    with pytest.raises(TelegramUpdateError):
        parse_update(
            {
                "update_id": 44,
                "message": {
                    "from": {"id": 7},
                    "chat": {"id": 8},
                    "voice": {"file_id": "file-1", "duration": 0},
                },
            }
        )


class FakeTelegramMedia:
    def __init__(self, content=b"audio"):
        self.content = content
        self.downloads = 0

    def get_file(self, file_id):
        assert file_id == "file-1"
        return "voice/file-1.oga"

    def download_file(self, file_path, *, max_bytes):
        self.downloads += 1
        assert file_path == "voice/file-1.oga"
        return self.content


class FakeSpeechToText:
    def __init__(self, transcript):
        self.transcript = transcript
        self.calls = []

    def transcribe(self, audio: AudioInput):
        self.calls.append(audio)
        return self.transcript


def test_voice_transcriber_validates_downloads_and_delegates_to_provider():
    telegram = FakeTelegramMedia()
    provider = FakeSpeechToText(" remind me tomorrow at 9 ")
    service = TelegramVoiceTranscriber(
        telegram,
        provider,
        VoiceLimits(max_duration_seconds=30, max_bytes=100),
    )
    transcript = service.transcribe(
        IncomingVoice("file-1", "unique-1", "audio/ogg", duration_seconds=4, file_size=6)
    )
    assert transcript == "remind me tomorrow at 9"
    assert telegram.downloads == 1
    assert provider.calls[0].filename == "file-1.ogg"
    assert provider.calls[0].content == b"audio"


def test_empty_voice_transcript_is_rejected_before_agent_processing():
    service = TelegramVoiceTranscriber(
        FakeTelegramMedia(),
        FakeSpeechToText("  "),
        VoiceLimits(max_duration_seconds=30, max_bytes=100),
    )
    with pytest.raises(VoiceProcessingError):
        service.transcribe(IncomingVoice("file-1", None, "audio/ogg", 4, 6))


def test_voice_update_claim_is_idempotent_before_transcription(db):
    service = channel_service(db)
    message = incoming(50, sender="10", chat="10")
    message = IncomingMessage(
        channel=message.channel,
        external_update_id=message.external_update_id,
        external_user_id=message.external_user_id,
        external_chat_id=message.external_chat_id,
        voice=IncomingVoice("file-1", "unique-1", "audio/ogg", 4, 6),
    )

    assert service.claim(message) is not None
    assert service.claim(message) is None
    assert db.query(InboundEvent).count() == 1


def test_database_failure_does_not_report_onboarding_success(db, monkeypatch):
    service = channel_service(db)
    original_commit = db.commit

    def fail_commit():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        service.process(incoming(1, text="/start", display_name="Ada"))
    monkeypatch.setattr(db, "commit", original_commit)
