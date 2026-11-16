from dataclasses import dataclass

from app.channels.contracts import IncomingVoice
from app.config import Settings
from app.integrations.telegram.adapter import TelegramBotClient, TelegramMediaError
from app.stt.protocol import AudioInput, SpeechToTextError, SpeechToTextProvider


class VoiceProcessingError(RuntimeError):
    """A voice note cannot safely be converted into a text command."""


@dataclass(frozen=True)
class VoiceLimits:
    max_duration_seconds: int
    max_bytes: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "VoiceLimits":
        if settings.telegram_voice_max_duration_seconds <= 0:
            raise ValueError("TELEGRAM_VOICE_MAX_DURATION_SECONDS must be positive")
        if settings.telegram_voice_max_bytes <= 0:
            raise ValueError("TELEGRAM_VOICE_MAX_BYTES must be positive")
        return cls(
            max_duration_seconds=settings.telegram_voice_max_duration_seconds,
            max_bytes=settings.telegram_voice_max_bytes,
        )


class TelegramVoiceTranscriber:
    """Validates Telegram voice metadata, downloads it, and delegates to an STT provider."""

    def __init__(
        self,
        telegram: TelegramBotClient,
        provider: SpeechToTextProvider,
        limits: VoiceLimits,
    ):
        self.telegram = telegram
        self.provider = provider
        self.limits = limits

    def transcribe(self, voice: IncomingVoice) -> str:
        self._validate(voice)
        try:
            file_path = self.telegram.get_file(voice.file_id)
            audio = self.telegram.download_file(file_path, max_bytes=self.limits.max_bytes)
            transcript = self.provider.transcribe(
                AudioInput(
                    content=audio,
                    filename=self._filename(file_path, voice.mime_type),
                    mime_type=voice.mime_type,
                )
            )
            if not isinstance(transcript, str):
                raise VoiceProcessingError(
                    "The speech transcription provider returned no transcript"
                )
        except (TelegramMediaError, SpeechToTextError, ValueError) as exc:
            raise VoiceProcessingError(str(exc)) from exc
        except Exception as exc:
            # Provider implementations must not leak SDK-specific failures through the webhook.
            raise VoiceProcessingError("Voice transcription failed") from exc
        transcript = transcript.strip()
        if not transcript or not any(character.isalnum() for character in transcript):
            raise VoiceProcessingError("The voice note did not contain understandable speech")
        return transcript

    def _validate(self, voice: IncomingVoice) -> None:
        if (
            not isinstance(voice.file_id, str)
            or not voice.file_id.strip()
            or not isinstance(voice.duration_seconds, int)
            or isinstance(voice.duration_seconds, bool)
            or voice.duration_seconds <= 0
        ):
            raise VoiceProcessingError("Telegram voice metadata is invalid")
        if voice.file_size is not None and (
            not isinstance(voice.file_size, int)
            or isinstance(voice.file_size, bool)
            or voice.file_size <= 0
        ):
            raise VoiceProcessingError("Telegram voice metadata is invalid")
        if voice.duration_seconds > self.limits.max_duration_seconds:
            raise VoiceProcessingError("Voice note is longer than the supported limit")
        if voice.file_size is not None and voice.file_size > self.limits.max_bytes:
            raise VoiceProcessingError("Voice note is larger than the supported limit")
        mime_type = voice.mime_type.casefold().split(";", 1)[0].strip() if voice.mime_type else None
        if mime_type and mime_type not in {"audio/ogg", "audio/opus"}:
            raise VoiceProcessingError(
                "Please send a Telegram voice note, not another audio format"
            )

    @staticmethod
    def _filename(file_path: str, mime_type: str | None = None) -> str:
        filename = file_path.rsplit("/", 1)[-1].strip()
        if not filename:
            return "voice.ogg"
        # Telegram commonly uses .oga for OGG/Opus voice notes; Groq expects .ogg.
        if filename.casefold().endswith(".oga"):
            return filename[:-4] + ".ogg"
        if "." not in filename and (not mime_type or mime_type.casefold().startswith("audio/")):
            return filename + ".ogg"
        return filename
