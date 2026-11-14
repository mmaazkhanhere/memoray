class ReminderInPastError(ValueError):
    """The requested reminder time is not strictly in the future."""


class DuplicateReminderError(ValueError):
    """An equivalent active reminder already exists."""

