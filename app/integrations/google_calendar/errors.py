class GoogleCalendarError(RuntimeError):
    """Base error for optional Calendar integration failures."""


class CalendarNotConfigured(GoogleCalendarError):
    pass


class OAuthStateError(GoogleCalendarError):
    pass


class GoogleCalendarApiError(GoogleCalendarError):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
