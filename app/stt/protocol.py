from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AudioInput:
    content: bytes
    filename: str
    mime_type: str | None = None


class SpeechToTextError(RuntimeError):
    pass


class SpeechToTextProvider(Protocol):
    def transcribe(self, audio: AudioInput) -> str:
        """Return the spoken text, or raise SpeechToTextError on provider failure."""
