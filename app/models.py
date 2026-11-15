import enum
import uuid
from datetime import date, datetime, time

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Index, String, Text, Time, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class UserStatus(str, enum.Enum):
    active = "active"
    pending = "pending"


class ReminderStatus(str, enum.Enum):
    active = "active"
    completed = "completed"
    canceled = "canceled"


class OccurrenceStatus(str, enum.Enum):
    pending = "pending"
    notified = "notified"
    completed = "completed"
    canceled = "canceled"


class DeliveryChannel(str, enum.Enum):
    in_app = "in_app"
    email = "email"
    sms = "sms"
    webhook = "webhook"


class RecurrenceFrequency(str, enum.Enum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"
    yearly = "yearly"


class ConversationStatus(str, enum.Enum):
    active = "active"
    completed = "completed"
    canceled = "canceled"


class DraftStatus(str, enum.Enum):
    collecting = "collecting"
    ready = "ready"
    created = "created"
    canceled = "canceled"


class ConversationMessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_token_hash: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status"), default=UserStatus.active, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    reminders: Mapped[list["Reminder"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    conversations: Mapped[list["ReminderConversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    destinations: Mapped[list["DeliveryDestination"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class DeliveryDestination(Base):
    __tablename__ = "delivery_destinations"
    __table_args__ = (
        Index("ix_destinations_user_channel", "user_id", "channel"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[DeliveryChannel] = mapped_column(
        Enum(DeliveryChannel, name="delivery_channel"), nullable=False
    )
    identity: Mapped[str] = mapped_column(String(500), nullable=False)
    is_linked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="destinations")


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        Index("ix_reminders_user_status_schedule", "user_id", "status", "schedule"),
        Index(
            "uq_active_reminders_user_title_schedule",
            "user_id",
            "normalized_title",
            "schedule",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(500), nullable=False)
    schedule: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recurrence_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    recurrence_frequency: Mapped[RecurrenceFrequency | None] = mapped_column(
        Enum(RecurrenceFrequency, name="recurrence_frequency"), nullable=True
    )
    recurrence_interval: Mapped[int] = mapped_column(default=1, nullable=False)
    destination_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("delivery_destinations.id", ondelete="SET NULL"), nullable=True
    )
    series_anchor_schedule: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[ReminderStatus] = mapped_column(
        Enum(ReminderStatus, name="reminder_status"), default=ReminderStatus.active, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    user: Mapped[User] = relationship(back_populates="reminders")
    destination: Mapped[DeliveryDestination | None] = relationship()
    occurrences: Mapped[list["ReminderOccurrence"]] = relationship(
        back_populates="reminder", cascade="all, delete-orphan", order_by="ReminderOccurrence.schedule"
    )


class ReminderOccurrence(Base):
    __tablename__ = "reminder_occurrences"
    __table_args__ = (Index("ix_occurrences_reminder_status_due", "reminder_id", "status", "next_due_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reminder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False)
    schedule: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[OccurrenceStatus] = mapped_column(
        Enum(OccurrenceStatus, name="occurrence_status"), default=OccurrenceStatus.pending, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    reminder: Mapped[Reminder] = relationship(back_populates="occurrences")
    series_schedule: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReminderConversation(Base):
    __tablename__ = "reminder_conversations"
    __table_args__ = (Index("ix_conversations_user_status_updated", "user_id", "status", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[ConversationStatus] = mapped_column(
        Enum(ConversationStatus, name="conversation_status"),
        default=ConversationStatus.active,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list["ReminderConversationMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="ReminderConversationMessage.created_at"
    )
    draft: Mapped["ReminderDraft | None"] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", uselist=False
    )


class ReminderConversationMessage(Base):
    __tablename__ = "reminder_conversation_messages"
    __table_args__ = (Index("ix_conversation_messages_conversation_created", "conversation_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reminder_conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[ConversationMessageRole] = mapped_column(
        Enum(ConversationMessageRole, name="conversation_message_role"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped[ReminderConversation] = relationship(back_populates="messages")


class ReminderDraft(Base):
    __tablename__ = "reminder_drafts"
    __table_args__ = (Index("ix_drafts_conversation_status", "conversation_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reminder_conversations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    schedule_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    schedule_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recurrence_frequency: Mapped[RecurrenceFrequency | None] = mapped_column(
        Enum(RecurrenceFrequency, name="recurrence_frequency"), nullable=True
    )
    recurrence_interval: Mapped[int] = mapped_column(default=1, nullable=False)
    status: Mapped[DraftStatus] = mapped_column(
        Enum(DraftStatus, name="draft_status"), default=DraftStatus.collecting, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    conversation: Mapped[ReminderConversation] = relationship(back_populates="draft")

