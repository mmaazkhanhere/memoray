import logging

from app.config import Settings
from app.stt.protocol import AudioInput, SpeechToTextError

logger = logging.getLogger(__name__)


class GroqSpeechToText:
    """Groq Whisper adapter implementing the provider-neutral STT contract."""

    def __init__(self, settings: Settings):
        api_key = settings.stt_api_key or settings.groq_api_key
        if not api_key:
            raise ValueError(
                "STT_API_KEY or GROQ_API_KEY is required for Groq speech transcription"
            )
        from groq import Groq

        self.client = Groq(api_key=api_key)
        self.model = settings.stt_model

    def transcribe(self, audio: AudioInput) -> str:
        try:
            response = self.client.audio.transcriptions.create(
                model=self.model,
                file=(audio.filename, audio.content),
                response_format="text",
            )
        except Exception as exc:
            logger.warning("Groq speech transcription failed: %s", exc)
            raise SpeechToTextError(
                "The speech transcription provider rejected the audio"
            ) from exc
        if isinstance(response, str):
            return response
        text = getattr(response, "text", None)
        if text is None and isinstance(response, dict):
            text = response.get("text")
        if not isinstance(text, str):
            raise SpeechToTextError("The speech transcription provider returned no transcript")
        return text
