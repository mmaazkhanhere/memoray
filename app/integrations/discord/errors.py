class DiscordNotConfigured(RuntimeError):
    pass


class DiscordOAuthError(RuntimeError):
    pass


class DiscordDeliveryError(RuntimeError):
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
