from dataclasses import dataclass


@dataclass(frozen=True)
class IncomingContact:
    phone_number: str | None
    owner_external_id: str | None


@dataclass(frozen=True)
class IncomingMessage:
    channel: str
    external_update_id: str
    external_user_id: str
    external_chat_id: str
    text: str | None = None
    display_name: str | None = None
    contact: IncomingContact | None = None


@dataclass(frozen=True)
class OutgoingMessage:
    channel: str
    external_chat_id: str
    text: str
    request_phone: bool = False
