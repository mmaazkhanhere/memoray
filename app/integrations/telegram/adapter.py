import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.channels.contracts import IncomingContact, IncomingMessage


class TelegramUpdateError(ValueError):
    pass


class TelegramBotClient:
    def __init__(self, token: str, *, opener=urlopen):
        self.token = token
        self.opener = opener

    def send_message(self, chat_id: str, text: str, *, request_phone: bool = False) -> None:
        payload = {"chat_id": chat_id, "text": text}
        if request_phone:
            payload["reply_markup"] = {
                "keyboard": [
                    [{"text": "Share phone number", "request_contact": True}],
                    [{"text": "Skip for now"}],
                ],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            }
        else:
            payload["reply_markup"] = {"remove_keyboard": True}
        request = Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("Telegram message delivery failed") from exc
        if not result.get("ok"):
            raise RuntimeError("Telegram rejected the message")


def parse_update(payload: dict) -> IncomingMessage:
    if not isinstance(payload, dict) or "update_id" not in payload:
        raise TelegramUpdateError("Telegram update_id is required")
    message = payload.get("message")
    if not isinstance(message, dict):
        raise TelegramUpdateError("unsupported Telegram update")
    sender = message.get("from")
    chat = message.get("chat")
    if not isinstance(sender, dict) or sender.get("id") is None:
        raise TelegramUpdateError("Telegram sender is required")
    if not isinstance(chat, dict) or chat.get("id") is None:
        raise TelegramUpdateError("Telegram chat is required")
    first = str(sender.get("first_name") or "").strip()
    last = str(sender.get("last_name") or "").strip()
    display_name = " ".join(part for part in (first, last) if part) or None
    contact_data = message.get("contact")
    contact = None
    if isinstance(contact_data, dict):
        owner_id = contact_data.get("user_id")
        contact = IncomingContact(
            phone_number=str(contact_data.get("phone_number") or "") or None,
            owner_external_id=str(owner_id) if owner_id is not None else None,
        )
    text = message.get("text")
    return IncomingMessage(
        channel="telegram",
        external_update_id=str(payload["update_id"]),
        external_user_id=str(sender["id"]),
        external_chat_id=str(chat["id"]),
        text=str(text) if text is not None else None,
        display_name=display_name,
        contact=contact,
    )
