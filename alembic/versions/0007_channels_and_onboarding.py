"""add channel accounts, onboarding, phone links, and message idempotency

Revision ID: 0007_channels_and_onboarding
Revises: 0006_memories
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_channels_and_onboarding"
down_revision = "0006_memories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    channel_kind = sa.Enum("telegram", name="channel_kind")
    channel_kind.create(bind, checkfirst=True)
    account_state = sa.Enum(
        "needs_name", "needs_timezone", "needs_phone_decision", "completed",
        name="channel_account_state",
    )
    account_state.create(bind, checkfirst=True)
    inbound_channel_kind = sa.Enum("telegram", name="inbound_channel_kind")
    inbound_channel_kind.create(bind, checkfirst=True)
    outbound_channel_kind = sa.Enum("telegram", name="outbound_channel_kind")
    outbound_channel_kind.create(bind, checkfirst=True)
    inbound_status = sa.Enum("processing", "completed", name="inbound_event_status")
    inbound_status.create(bind, checkfirst=True)

    # Existing users are complete, so making these columns nullable is a
    # backwards-compatible way to represent a newly created pending account.
    op.alter_column("users", "name", existing_type=sa.String(length=200), nullable=True)
    op.alter_column("users", "timezone", existing_type=sa.String(length=64), nullable=True)

    op.create_table(
        "channel_accounts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "channel",
            sa.Enum("telegram", name="channel_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("external_user_id", sa.String(length=200), nullable=False),
        sa.Column("external_chat_id", sa.String(length=200), nullable=False),
        sa.Column(
            "onboarding_state",
            sa.Enum(
                "needs_name", "needs_timezone", "needs_phone_decision", "completed",
                name="channel_account_state", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["reminder_conversations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_channel_accounts_channel_external_user",
        "channel_accounts", ["channel", "external_user_id"], unique=True,
    )
    op.create_index("ix_channel_accounts_user_channel", "channel_accounts", ["user_id", "channel"])

    op.create_table(
        "phone_links",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("e164_number", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_active_phone_links_e164", "phone_links", ["e164_number"], unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index("ix_phone_links_user_active", "phone_links", ["user_id", "is_active"])

    op.create_table(
        "inbound_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "channel",
            sa.Enum("telegram", name="inbound_channel_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("external_update_id", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.Enum("processing", "completed", name="inbound_event_status", create_type=False),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_inbound_events_channel_external_id",
        "inbound_events", ["channel", "external_update_id"], unique=True,
    )

    op.create_table(
        "outbound_messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("inbound_event_id", sa.UUID(), nullable=True),
        sa.Column(
            "channel",
            sa.Enum("telegram", name="outbound_channel_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("external_chat_id", sa.String(length=200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("request_phone", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["inbound_event_id"], ["inbound_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbound_messages_pending", "outbound_messages", ["channel", "sent_at"])


def downgrade() -> None:
    op.drop_index("ix_outbound_messages_pending", table_name="outbound_messages")
    op.drop_table("outbound_messages")
    op.drop_index("uq_inbound_events_channel_external_id", table_name="inbound_events")
    op.drop_table("inbound_events")
    op.drop_index("ix_phone_links_user_active", table_name="phone_links")
    op.drop_index("uq_active_phone_links_e164", table_name="phone_links")
    op.drop_table("phone_links")
    op.drop_index("ix_channel_accounts_user_channel", table_name="channel_accounts")
    op.drop_index("uq_channel_accounts_channel_external_user", table_name="channel_accounts")
    op.drop_table("channel_accounts")
    op.alter_column("users", "timezone", existing_type=sa.String(length=64), nullable=False)
    op.alter_column("users", "name", existing_type=sa.String(length=200), nullable=False)
    sa.Enum(name="inbound_event_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="outbound_channel_kind").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="inbound_channel_kind").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="channel_account_state").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="channel_kind").drop(op.get_bind(), checkfirst=True)
