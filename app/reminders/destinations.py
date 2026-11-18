import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelIdentity,
    ChannelIdentityStatus,
    ChannelKind,
    DeliveryChannel,
    DeliveryDestination,
)
from app.reminders.errors import InvalidDestinationError
from app.schemas import DeliveryDestinationCreate


class DestinationService:
    """Owns destination linkage and channel validation."""

    def __init__(self, db: Session):
        self.db = db

    def create(self, user_id: uuid.UUID, data: DeliveryDestinationCreate) -> DeliveryDestination:
        identity = data.identity.strip()
        if not identity:
            raise ValueError("destination identity must not be blank")
        self._validate_identity(data.channel, identity)
        destination = DeliveryDestination(
            user_id=user_id,
            channel=data.channel,
            identity=identity,
            is_linked=data.is_linked,
            is_valid=data.is_valid,
        )
        self.db.add(destination)
        self.db.commit()
        self.db.refresh(destination)
        return destination

    def get_owned_valid(
        self,
        user_id: uuid.UUID,
        destination_id: uuid.UUID,
        *,
        channel: DeliveryChannel | None = None,
    ) -> DeliveryDestination:
        destination = self.db.scalar(
            select(DeliveryDestination).where(
                DeliveryDestination.id == destination_id,
                DeliveryDestination.user_id == user_id,
            )
        )
        if destination is None:
            raise InvalidDestinationError("destination not found for user")
        if not destination.is_linked or not destination.is_valid:
            raise InvalidDestinationError("destination is not linked and valid")
        if channel is not None and destination.channel != channel:
            raise InvalidDestinationError("destination is not valid for this channel")
        return destination

    def default_for_user(self, user_id: uuid.UUID) -> DeliveryDestination:
        destination = self.db.scalar(
            select(DeliveryDestination)
            .where(
                DeliveryDestination.user_id == user_id,
                DeliveryDestination.channel == DeliveryChannel.in_app,
                DeliveryDestination.is_linked.is_(True),
                DeliveryDestination.is_valid.is_(True),
            )
            .order_by(DeliveryDestination.created_at.asc())
        )
        if destination is None:
            destination = DeliveryDestination(
                user_id=user_id,
                channel=DeliveryChannel.in_app,
                identity=f"user:{user_id}",
                is_linked=True,
                is_valid=True,
            )
            self.db.add(destination)
            self.db.flush()
        return destination

    @staticmethod
    def _validate_identity(channel: DeliveryChannel, identity: str) -> None:
        if channel == DeliveryChannel.email and ("@" not in identity or " " in identity):
            raise ValueError("email destination identity is invalid")
        if channel == DeliveryChannel.webhook and not identity.startswith(("http://", "https://")):
            raise ValueError("webhook destination identity must be an http URL")
        if channel == DeliveryChannel.sms and not any(char.isdigit() for char in identity):
            raise ValueError("sms destination identity is invalid")


def default_telegram_identity(db: Session, user_id: uuid.UUID) -> ChannelIdentity | None:
    """Return the user's linked Telegram identity used for default deliveries."""
    return db.scalar(
        select(ChannelIdentity)
        .where(
            ChannelIdentity.user_id == user_id,
            ChannelIdentity.channel == ChannelKind.telegram,
            ChannelIdentity.status == ChannelIdentityStatus.linked,
        )
        .order_by(ChannelIdentity.is_default.desc(), ChannelIdentity.created_at.asc())
    )
