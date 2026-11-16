import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.channels.contracts import IncomingContact, IncomingMessage, IncomingVoice


class TelegramUpdateError(ValueError):
    pass


class TelegramDeliveryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        classification: str = "retryable",
        retry_after: float | None = None,
        invalidate_identity: bool = False,
    ):
        super().__init__(message)
        self.classification = classification
        self.retry_after = retry_after
        self.invalidate_identity = invalidate_identity


class TelegramMediaError(RuntimeError):
    """Telegram media could not be fetched or did not pass local limits."""


class TelegramBotClient:
    def __init__(self, token: str, *, opener=urlopen):
        self.token = token
        self.opener = opener

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        request_phone: bool = False,
        idempotency_key: str | None = None,
    ) -> None:
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
        headers = {"Content-Type": "application/json"}
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        request = Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self.opener(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            retry_after = None
            try:
                retry_after = float(exc.headers.get("Retry-After"))
            except (TypeError, ValueError):
                pass
            if exc.code == 429:
                raise TelegramDeliveryError(
                    "Telegram rate limit",
                    classification="retryable",
                    retry_after=retry_after,
                ) from exc
            if 500 <= exc.code < 600:
                raise TelegramDeliveryError("Telegram server error", classification="retryable") from exc
            raise TelegramDeliveryError(
                f"Telegram rejected request with HTTP {exc.code}",
                classification="permanent",
                invalidate_identity=exc.code in {400, 403, 404},
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TelegramDeliveryError("Telegram network result was uncertain", classification="unknown") from exc
        if not result.get("ok"):
            description = str(result.get("description") or "Telegram rejected the message")
            parameters = result.get("parameters") or {}
            retry_after = parameters.get("retry_after")
            if result.get("error_code") == 429:
                raise TelegramDeliveryError(
                    description, classification="retryable", retry_after=retry_after
                )
            raise TelegramDeliveryError(
                description,
                classification="permanent",
                invalidate_identity=True,
            )

    def get_file(self, file_id: str) -> str:
        if not file_id.strip():
            raise TelegramMediaError("Telegram voice file id is required")
        request = Request(
            f"https://api.telegram.org/bot{self.token}/getFile?file_id={quote(file_id, safe='')}",
            method="GET",
        )
        try:
            with self.opener(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise TelegramMediaError("Telegram voice file could not be located") from exc
        if not isinstance(result, dict):
            raise TelegramMediaError("Telegram returned an invalid file response")
        result_data = result.get("result")
        file_path = (
            result_data.get("file_path")
            if result.get("ok") and isinstance(result_data, dict)
            else None
        )
        if not isinstance(file_path, str) or not file_path:
            raise TelegramMediaError("Telegram did not return a voice file path")
        return file_path

    def download_file(self, file_path: str, *, max_bytes: int) -> bytes:
        if not file_path or file_path.startswith(("/", "\\")) or ".." in file_path.split("/"):
            raise TelegramMediaError("Telegram returned an invalid voice file path")
        request = Request(
            f"https://api.telegram.org/file/bot{self.token}/{file_path}", method="GET"
        )
        try:
            with self.opener(request, timeout=30) as response:
                chunks: list[bytes] = []
                total = 0
                while True:
                    try:
                        chunk = response.read(64 * 1024)
                    except TypeError:
                        # Small test doubles and some urllib-compatible streams expose read().
                        chunk = response.read()
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise TelegramMediaError("Telegram voice note is too large")
                    chunks.append(chunk)
        except TelegramMediaError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise TelegramMediaError("Telegram voice note could not be downloaded") from exc
        return b"".join(chunks)


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
    voice_data = message.get("voice")
    unsupported_media = any(
        key in message for key in ("audio", "document", "photo", "video", "video_note", "animation")
    )
    if text is None and contact_data is None and voice_data is None and unsupported_media:
        raise TelegramUpdateError("unsupported Telegram media; send a voice note or text message")
    voice = None
    if voice_data is not None:
        if not isinstance(voice_data, dict):
            raise TelegramUpdateError("Telegram voice metadata is invalid")
        file_id = str(voice_data.get("file_id") or "").strip()
        duration = voice_data.get("duration")
        if (
            not file_id
            or not isinstance(duration, int)
            or isinstance(duration, bool)
            or duration <= 0
        ):
            raise TelegramUpdateError("Telegram voice file_id and positive duration are required")
        file_size = voice_data.get("file_size")
        if file_size is not None and (
            not isinstance(file_size, int) or isinstance(file_size, bool) or file_size <= 0
        ):
            raise TelegramUpdateError("Telegram voice file_size is invalid")
        voice = IncomingVoice(
            file_id=file_id,
            file_unique_id=str(voice_data.get("file_unique_id") or "") or None,
            mime_type=str(voice_data.get("mime_type") or "") or None,
            duration_seconds=duration,
            file_size=file_size,
        )
        if text is not None:
            raise TelegramUpdateError("Telegram update cannot contain both text and voice")
    return IncomingMessage(
        channel="telegram",
        external_update_id=str(payload["update_id"]),
        external_user_id=str(sender["id"]),
        external_chat_id=str(chat["id"]),
        text=str(text) if text is not None else None,
        display_name=display_name,
        contact=contact,
        voice=voice,
    )
