from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {value}") from exc
    return value


def to_utc(value: datetime, timezone_name: str) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(timezone_name))
    return value.astimezone(timezone.utc)


def to_user_timezone(value: datetime | None, timezone_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(timezone_name))


def format_user_datetime(value: datetime, timezone_name: str) -> str:
    localized = to_user_timezone(value, timezone_name)
    assert localized is not None
    hour = localized.strftime("%I").lstrip("0") or "12"
    return f"{localized.strftime('%A, %B')} {localized.day} at {hour}:{localized:%M} {localized:%p} ({timezone_name})"

