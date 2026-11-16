"""add channel identities and leased durable notification delivery"""

from alembic import op
import sqlalchemy as sa


revision = "0008_durable_notification_worker"
down_revision = "0007_channels_and_onboarding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("ALTER TYPE delivery_channel ADD VALUE IF NOT EXISTS 'telegram'")
    op.execute("ALTER TYPE delivery_channel ADD VALUE IF NOT EXISTS 'phone'")
    op.add_column("inbound_events", sa.Column("payload_hash", sa.String(length=64), nullable=True))

    identity_channel_kind = sa.Enum("telegram", "phone", name="identity_channel_kind")
    identity_channel_kind.create(bind, checkfirst=True)
    identity_status = sa.Enum("linked", "unlinked", "invalid", name="channel_identity_status")
    identity_status.create(bind, checkfirst=True)
    notification_status = sa.Enum(
        "pending", "sending", "sent", "failed", "unknown", "canceled",
        name="notification_status",
    )
    notification_status.create(bind, checkfirst=True)

    op.create_table(
        "channel_identities",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.Enum("telegram", "phone", name="identity_channel_kind", create_type=False), nullable=False),
        sa.Column("external_principal_id", sa.String(length=200), nullable=False),
        sa.Column("external_destination_id", sa.String(length=200), nullable=False),
        sa.Column("status", sa.Enum("linked", "unlinked", "invalid", name="channel_identity_status", create_type=False), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_channel_identities_channel_principal", "channel_identities", ["channel", "external_principal_id"], unique=True)
    op.create_index(
        "uq_channel_identities_user_default", "channel_identities", ["user_id", "channel"], unique=True,
        postgresql_where=sa.text("is_default = true AND status = 'linked'"),
    )
    op.create_index("ix_channel_identities_user_channel_status", "channel_identities", ["user_id", "channel", "status"])

    op.execute(
        """
        INSERT INTO channel_identities
            (id, user_id, channel, external_principal_id, external_destination_id,
             status, is_default, verified_at)
        SELECT id, user_id, 'telegram', external_user_id, external_chat_id,
               'linked', TRUE, created_at
        FROM channel_accounts
        ON CONFLICT (channel, external_principal_id) DO UPDATE
        SET external_destination_id = EXCLUDED.external_destination_id
        """
    )
    op.execute(
        """
        INSERT INTO channel_identities
            (id, user_id, channel, external_principal_id, external_destination_id,
             status, is_default, verified_at)
        SELECT id, user_id, 'phone', e164_number, e164_number,
               'linked',
               (ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at, id) = 1),
               created_at
        FROM phone_links
        WHERE is_active = TRUE
        ON CONFLICT (channel, external_principal_id) DO UPDATE
        SET external_destination_id = EXCLUDED.external_destination_id
        """
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("reminder_id", sa.UUID(), nullable=False),
        sa.Column("occurrence_id", sa.UUID(), nullable=False),
        sa.Column("channel_identity_id", sa.UUID(), nullable=True),
        sa.Column("destination_id", sa.UUID(), nullable=True),
        sa.Column("channel", sa.Enum("in_app", "email", "sms", "webhook", "telegram", "phone", name="delivery_channel", create_type=False), nullable=False),
        sa.Column("destination_snapshot", sa.String(length=500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("provider_idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_phone", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("delivery_sequence", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.Enum("pending", "sending", "sent", "failed", "unknown", "canceled", name="notification_status", create_type=False), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uncertain_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("worker_id", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["occurrence_id"], ["reminder_occurrences.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["channel_identity_id"], ["channel_identities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["destination_id"], ["delivery_destinations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_idempotency_key"),
    )
    op.create_index("ix_notification_deliveries_due", "notification_deliveries", ["status", "next_attempt_at"])
    op.create_index("ix_notification_deliveries_identity_status", "notification_deliveries", ["channel_identity_id", "status"])
    op.create_index("uq_notification_deliveries_occurrence_sequence", "notification_deliveries", ["occurrence_id", "delivery_sequence"], unique=True)

    op.create_table(
        "notification_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("notification_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=200), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("classification", sa.String(length=32), nullable=True),
        sa.Column("provider_reference", sa.String(length=200), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["notification_id"], ["notification_deliveries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("notification_id", "sequence"),
    )


def downgrade() -> None:
    op.drop_table("notification_attempts")
    op.drop_index("uq_notification_deliveries_occurrence_sequence", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_identity_status", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_due", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_index("ix_channel_identities_user_channel_status", table_name="channel_identities")
    op.drop_index("uq_channel_identities_user_default", table_name="channel_identities")
    op.drop_index("uq_channel_identities_channel_principal", table_name="channel_identities")
    op.drop_table("channel_identities")
    sa.Enum(name="notification_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="channel_identity_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="identity_channel_kind").drop(op.get_bind(), checkfirst=True)
    op.drop_column("inbound_events", "payload_hash")
