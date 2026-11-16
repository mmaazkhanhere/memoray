import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class IncomingContact:
    phone_number: str | None
    owner_external_id: str | None


@dataclass(frozen=True)
class IncomingVoice:
    """Telegram voice metadata; audio bytes are fetched only by the Telegram adapter."""

    file_id: str
    file_unique_id: str | None
    mime_type: str | None
    duration_seconds: int
    file_size: int | None = None


@dataclass(frozen=True)
class IncomingMessage:
    channel: str
    external_update_id: str
    external_user_id: str
    external_chat_id: str
    text: str | None = None
    display_name: str | None = None
    contact: IncomingContact | None = None
    voice: IncomingVoice | None = None
    # Telegram callback queries use the same inbound pipeline as text updates.
    # Keeping the parsed occurrence ID here means channel routing does not need
    # to understand Telegram's raw payload shape.
    completion_occurrence_id: uuid.UUID | None = None
    callback_query_id: str | None = None
    callback_message_id: int | None = None


@dataclass(frozen=True)
class OutgoingMessage:
    channel: str
    external_chat_id: str
    text: str
    request_phone: bool = False
