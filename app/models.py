import enum
import uuid
from datetime import date, datetime, time

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, ForeignKey, Index, String, Text, Time, func, text
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


class NotificationStatus(str, enum.Enum):
    pending = "pending"
    sending = "sending"
    sent = "sent"
    failed = "failed"
    unknown = "unknown"
    canceled = "canceled"


class DeliveryChannel(str, enum.Enum):
    in_app = "in_app"
    email = "email"
    sms = "sms"
    webhook = "webhook"
    telegram = "telegram"
    discord = "discord"
    phone = "phone"


class ChannelKind(str, enum.Enum):
    telegram = "telegram"
    discord = "discord"
    phone = "phone"


class ChannelIdentityStatus(str, enum.Enum):
    linked = "linked"
    unlinked = "unlinked"
    invalid = "invalid"


class VapiCallStatus(str, enum.Enum):
    active = "active"
    ended = "ended"


class ChannelAccountState(str, enum.Enum):
    needs_name = "needs_name"
    needs_timezone = "needs_timezone"
    needs_phone_decision = "needs_phone_decision"
    completed = "completed"


class InboundEventStatus(str, enum.Enum):
    processing = "processing"
    completed = "completed"


class ReminderChangeStatus(str, enum.Enum):
    pending = "pending"
    sending = "sending"
    completed = "completed"
    failed = "failed"


class GoogleCalendarConnectionStatus(str, enum.Enum):
    active = "active"
    reauthorization_required = "reauthorization_required"
    revoked = "revoked"


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
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    channel_accounts: Mapped[list["ChannelAccount"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    phone_links: Mapped[list["PhoneLink"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    channel_identities: Mapped[list["ChannelIdentity"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    vapi_calls: Mapped[list["VapiCall"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ChannelAccount(Base):
    """Maps an external channel identity to exactly one internal user."""

    __tablename__ = "channel_accounts"
    __table_args__ = (
        Index(
            "uq_channel_accounts_channel_external_user",
            "channel",
            "external_user_id",
            unique=True,
        ),
        Index("ix_channel_accounts_user_channel", "user_id", "channel"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[ChannelKind] = mapped_column(Enum(ChannelKind, name="channel_kind"), nullable=False)
    external_user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    external_chat_id: Mapped[str] = mapped_column(String(200), nullable=False)
    pending_mcp_nonce: Mapped[str | None] = mapped_column(String(200), nullable=True)
    onboarding_state: Mapped[ChannelAccountState] = mapped_column(
        Enum(ChannelAccountState, name="channel_account_state"),
        default=ChannelAccountState.needs_timezone,
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reminder_conversations.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="channel_accounts")


class PhoneLink(Base):
    __tablename__ = "phone_links"
    __table_args__ = (
        Index(
            "uq_active_phone_links_e164",
            "e164_number",
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
        Index("ix_phone_links_user_active", "user_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    e164_number: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="phone_links")


class ChannelIdentity(Base):
    """An authenticated inbound principal and its outbound destination."""

    __tablename__ = "channel_identities"
    __table_args__ = (
        Index("uq_channel_identities_channel_principal", "channel", "external_principal_id", unique=True),
        Index(
            "uq_channel_identities_user_default",
            "user_id",
            "channel",
            unique=True,
            postgresql_where=text("is_default = true AND status = 'linked'"),
            sqlite_where=text("is_default = 1 AND status = 'linked'"),
        ),
        Index("ix_channel_identities_user_channel_status", "user_id", "channel", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[ChannelKind] = mapped_column(
        Enum(ChannelKind, name="identity_channel_kind"), nullable=False
    )
    external_principal_id: Mapped[str] = mapped_column(String(200), nullable=False)
    external_destination_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[ChannelIdentityStatus] = mapped_column(
        Enum(ChannelIdentityStatus, name="channel_identity_status"),
        default=ChannelIdentityStatus.linked,
        nullable=False,
    )
    is_default: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="channel_identities")


class VapiCall(Base):
    """Authenticated state for one inbound Vapi call."""

    __tablename__ = "vapi_calls"
    __table_args__ = (
        Index("uq_vapi_calls_external_id", "external_call_id", unique=True),
        Index("ix_vapi_calls_user_status", "user_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    external_call_id: Mapped[str] = mapped_column(String(200), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    caller_e164: Mapped[str] = mapped_column(String(32), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reminder_conversations.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[VapiCallStatus] = mapped_column(
        Enum(VapiCallStatus, name="vapi_call_status"), default=VapiCallStatus.active, nullable=False
    )
    last_reminder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reminders.id", ondelete="SET NULL"), nullable=True
    )
    last_occurrence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reminder_occurrences.id", ondelete="SET NULL"), nullable=True
    )
    target_ambiguous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="vapi_calls")


class VapiToolExecution(Base):
    """Cached result for a Vapi function call, making provider retries safe."""

    __tablename__ = "vapi_tool_executions"
    __table_args__ = (
        Index(
            "uq_vapi_tool_executions_call_tool",
            "vapi_call_id",
            "external_tool_call_id",
            unique=True,
        ),
        Index("ix_vapi_tool_executions_call_created", "vapi_call_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    vapi_call_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vapi_calls.id", ondelete="CASCADE"), nullable=False
    )
    external_tool_call_id: Mapped[str] = mapped_column(String(200), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    call: Mapped[VapiCall] = relationship()


class InboundEvent(Base):
    __tablename__ = "inbound_events"
    __table_args__ = (
        Index("uq_inbound_events_channel_external_id", "channel", "external_update_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    channel: Mapped[ChannelKind] = mapped_column(
        Enum(ChannelKind, name="inbound_channel_kind"), nullable=False
    )
    external_update_id: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[InboundEventStatus] = mapped_column(
        Enum(InboundEventStatus, name="inbound_event_status"),
        default=InboundEventStatus.processing,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    __table_args__ = (Index("ix_outbound_messages_pending", "channel", "sent_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    inbound_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("inbound_events.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[ChannelKind] = mapped_column(
        Enum(ChannelKind, name="outbound_channel_kind"), nullable=False
    )
    external_chat_id: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    request_phone: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReminderChange(Base):
    """Durable notification that an occurrence's external projections may be stale."""

    __tablename__ = "reminder_changes"
    __table_args__ = (
        Index("ix_reminder_changes_due", "status", "next_attempt_at"),
        Index("ix_reminder_changes_occurrence", "occurrence_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reminder_occurrences.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[ReminderChangeStatus] = mapped_column(
        Enum(ReminderChangeStatus, name="reminder_change_status"),
        default=ReminderChangeStatus.pending,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    worker_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    occurrence: Mapped["ReminderOccurrence"] = relationship()


class GoogleCalendarConnection(Base):
    """One optional Google Calendar authorization for a user."""

    __tablename__ = "google_calendar_connections"
    __table_args__ = (Index("uq_google_calendar_connections_user", "user_id", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(500), default="primary", nullable=False)
    google_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    google_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[GoogleCalendarConnectionStatus] = mapped_column(
        Enum(GoogleCalendarConnectionStatus, name="google_calendar_connection_status"),
        default=GoogleCalendarConnectionStatus.active,
        nullable=False,
    )
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship()
    events: Mapped[list["GoogleCalendarEvent"]] = relationship(
        back_populates="connection", cascade="all, delete-orphan"
    )


class GoogleCalendarOAuthAttempt(Base):
    """Single-use OAuth state bound to one authenticated Memoray user."""

    __tablename__ = "google_calendar_oauth_attempts"
    __table_args__ = (Index("uq_google_calendar_oauth_attempts_state", "state_hash", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(200), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DiscordLinkAttempt(Base):
    """Single-use OAuth state used to link a Discord identity to a Memoray user."""

    __tablename__ = "discord_link_attempts"
    __table_args__ = (Index("uq_discord_link_attempts_state_hash", "state_hash", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(200), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class McpLinkAttempt(Base):
    """A short-lived Telegram approval for an MCP OAuth request."""

    __tablename__ = "mcp_link_attempts"
    __table_args__ = (Index("uq_mcp_link_attempts_nonce_hash", "nonce_hash", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    client_id: Mapped[str] = mapped_column(String(500), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(2000), nullable=False)
    state: Mapped[str] = mapped_column(String(2000), nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[str] = mapped_column(String(500), nullable=False)
    resource: Mapped[str] = mapped_column(String(2000), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    code_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class McpAuthorizationCode(Base):
    """Single-use OAuth authorization code; only its hash is persisted."""

    __tablename__ = "mcp_authorization_codes"
    __table_args__ = (Index("uq_mcp_authorization_codes_code_hash", "code_hash", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(500), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(2000), nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[str] = mapped_column(String(500), nullable=False)
    resource: Mapped[str] = mapped_column(String(2000), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class McpRefreshToken(Base):
    """Rotating refresh token; the bearer value is never stored."""

    __tablename__ = "mcp_refresh_tokens"
    __table_args__ = (Index("uq_mcp_refresh_tokens_token_hash", "token_hash", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(500), nullable=False)
    scope: Mapped[str] = mapped_column(String(500), nullable=False)
    resource: Mapped[str] = mapped_column(String(2000), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class McpOperationReceipt(Base):
    """Durable response cache for mutation retries from MCP clients."""

    __tablename__ = "mcp_operation_receipts"
    __table_args__ = (
        Index(
            "uq_mcp_operation_receipts_user_key",
            "user_id",
            "client_id",
            "operation_key",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(500), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(200), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GoogleCalendarEvent(Base):
    """Maps one reminder occurrence to its deterministic Google event ID."""

    __tablename__ = "google_calendar_events"
    __table_args__ = (
        Index("uq_google_calendar_events_connection_occurrence", "connection_id", "occurrence_id", unique=True),
        Index("uq_google_calendar_events_connection_external", "connection_id", "external_event_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("google_calendar_connections.id", ondelete="CASCADE"), nullable=False
    )
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reminder_occurrences.id", ondelete="CASCADE"), nullable=False
    )
    external_event_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    connection: Mapped[GoogleCalendarConnection] = relationship(back_populates="events")
    occurrence: Mapped["ReminderOccurrence"] = relationship()


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
    notification_deliveries: Mapped[list["NotificationDelivery"]] = relationship(
        back_populates="occurrence", cascade="all, delete-orphan"
    )


class NotificationDelivery(Base):
    """Durable delivery intent; network calls happen only after it is claimed."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        Index("ix_notification_deliveries_due", "status", "next_attempt_at"),
        Index("ix_notification_deliveries_identity_status", "channel_identity_id", "status"),
        Index("uq_notification_deliveries_occurrence_sequence", "occurrence_id", "delivery_sequence", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reminder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False)
    occurrence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reminder_occurrences.id", ondelete="CASCADE"), nullable=False)
    channel_identity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("channel_identities.id", ondelete="SET NULL"), nullable=True)
    destination_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("delivery_destinations.id", ondelete="SET NULL"), nullable=True)
    channel: Mapped[DeliveryChannel] = mapped_column(Enum(DeliveryChannel, name="delivery_channel", create_type=False), nullable=False)
    destination_snapshot: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    provider_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    request_phone: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delivery_sequence: Mapped[int] = mapped_column(default=1, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(Enum(NotificationStatus, name="notification_status"), default=NotificationStatus.pending, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(default=0, nullable=False)
    uncertain_attempt_count: Mapped[int] = mapped_column(default=0, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    reminder: Mapped[Reminder] = relationship()
    occurrence: Mapped[ReminderOccurrence] = relationship(back_populates="notification_deliveries")
    channel_identity: Mapped[ChannelIdentity | None] = relationship()
    destination: Mapped[DeliveryDestination | None] = relationship()
    attempts: Mapped[list["NotificationAttempt"]] = relationship(back_populates="notification", cascade="all, delete-orphan", order_by="NotificationAttempt.sequence")


class NotificationAttempt(Base):
    __tablename__ = "notification_attempts"
    __table_args__ = (Index("uq_notification_attempts_sequence", "notification_id", "sequence", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    notification_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notification_deliveries.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    worker_id: Mapped[str] = mapped_column(String(200), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    classification: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    notification: Mapped[NotificationDelivery] = relationship(back_populates="attempts")


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


# Keep the declarative registry complete for Alembic and local test databases while
# keeping memory behavior in the app.memories package.
from app.memories import models as _memory_models  # noqa: E402,F401

Memory = _memory_models.Memory
MemoryEmbeddingStatus = _memory_models.MemoryEmbeddingStatus

