import uuid
import re

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import PhoneLink


class PhoneLinkConflict(ValueError):
    """The number is already actively linked to another user."""


class PhoneService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def normalize_e164(raw_number: str | None) -> str:
        """Normalize a provider caller number without accepting local numbers."""
        if not raw_number:
            raise ValueError("phone number is required")
        value = re.sub(r"[\s().-]", "", str(raw_number))
        if value.startswith("00"):
            value = "+" + value[2:]
        if not re.fullmatch(r"\+[1-9]\d{7,14}", value):
            raise ValueError("phone number is invalid")
        return value

    @staticmethod
    def normalize_contact(*, sender_id: str, owner_external_id: str | None, raw_number: str | None) -> str:
        if not owner_external_id:
            raise ValueError("contact owner is required")
        if owner_external_id != sender_id:
            raise ValueError("contact belongs to another sender")
        if not raw_number:
            raise ValueError("contact phone number is required")
        try:
            candidate = str(raw_number).strip()
            # Telegram contact payloads may omit the leading plus while still
            # carrying a full international number (for example, 923001234567).
            if candidate and not candidate.startswith(("+", "00")):
                candidate = "+" + candidate
            return PhoneService.normalize_e164(candidate)
        except ValueError as exc:
            raise ValueError(
                "phone number is invalid; use an international number such as +14155550123"
            ) from exc

    def link(self, user_id: uuid.UUID, e164_number: str) -> PhoneLink:
        link = PhoneLink(user_id=user_id, e164_number=e164_number, is_active=True)
        try:
            # The channel service owns the outer transaction.  Flushing here
            # still lets the database's unique index arbitrate concurrent links.
            with self.db.begin_nested():
                self.db.add(link)
                self.db.flush()
        except IntegrityError as exc:
            raise PhoneLinkConflict("phone number is already linked to another active account") from exc
        return link
