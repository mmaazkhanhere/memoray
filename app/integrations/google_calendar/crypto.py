"""Token encryption kept private to the Calendar integration."""


class TokenCipher:
    def __init__(self, key: str):
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - dependency is declared in pyproject
            raise RuntimeError("cryptography is required for Google Calendar tokens") from exc
        try:
            self._fernet = Fernet(key.encode())
        except Exception as exc:
            raise ValueError("GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY is invalid") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode()).decode()
