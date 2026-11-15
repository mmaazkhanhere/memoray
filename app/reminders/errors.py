class ReminderInPastError(ValueError):
    """The requested reminder time is not strictly in the future."""


class DuplicateReminderError(ValueError):
    """An equivalent active reminder already exists."""


class ReminderNotActiveError(ValueError):
    """A mutation requires an active reminder."""


class OccurrenceNotPendingError(ValueError):
    """A reschedule target is not an editable future occurrence."""


class OccurrenceNotCancelableError(ValueError):
    """A cancellation target is not a pending occurrence."""


class OccurrenceNotNotifiedError(ValueError):
    """A snooze target has not been delivered."""


class InvalidDestinationError(ValueError):
    """The destination is not linked, valid, owned, or channel-compatible."""

