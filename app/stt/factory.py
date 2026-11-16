from app.config import Settings
from app.stt.groq import GroqSpeechToText
from app.stt.protocol import SpeechToTextProvider


def create_speech_to_text(settings: Settings) -> SpeechToTextProvider:
    provider = settings.stt_provider.strip().casefold()
    if provider == "groq":
        return GroqSpeechToText(settings)
    raise ValueError(f"Unsupported speech-to-text provider: {settings.stt_provider}")
