"""add urgent reminder call escalation state"""

from alembic import op
import sqlalchemy as sa


revision = "0014_urgent_reminders"
down_revision = "0013_discord_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reminders", sa.Column("is_urgent", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("reminder_drafts", sa.Column("is_urgent", sa.Boolean(), nullable=False, server_default=sa.false()))

    urgent_delivery_status = sa.Enum(
        "pending", "calling", "retry_pending", "delivered", "telegram_queued", "failed", "canceled",
        name="urgent_delivery_status",
    )
    urgent_call_status = sa.Enum(
        "dispatching", "ringing", "answered", "not_picked", "failed", name="urgent_call_status"
    )
    urgent_delivery_status.create(op.get_bind(), checkfirst=True)
    urgent_call_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "urgent_reminder_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("reminder_id", sa.UUID(), nullable=False),
        sa.Column("occurrence_id", sa.UUID(), nullable=False),
        sa.Column("phone_snapshot", sa.String(length=32), nullable=False),
        sa.Column("telegram_snapshot", sa.String(length=200), nullable=False),
        sa.Column("status", sa.Enum(
            "pending", "calling", "retry_pending", "delivered", "telegram_queued", "failed", "canceled",
            name="urgent_delivery_status", create_type=False,
        ), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("worker_id", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["occurrence_id"], ["reminder_occurrences.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_urgent_reminder_deliveries_occurrence", "urgent_reminder_deliveries", ["occurrence_id"], unique=True)
    op.create_index("ix_urgent_reminder_deliveries_due", "urgent_reminder_deliveries", ["status", "next_attempt_at"])

    op.create_table(
        "urgent_call_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("delivery_id", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("correlation_key", sa.String(length=200), nullable=False),
        sa.Column("external_call_id", sa.String(length=200), nullable=True),
        sa.Column("status", sa.Enum(
            "dispatching", "ringing", "answered", "not_picked", "failed",
            name="urgent_call_status", create_type=False,
        ), nullable=False),
        sa.Column("outcome", sa.String(length=100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["delivery_id"], ["urgent_reminder_deliveries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_urgent_call_attempts_external_id", "urgent_call_attempts", ["external_call_id"], unique=True)
    op.create_index("uq_urgent_call_attempts_correlation_key", "urgent_call_attempts", ["correlation_key"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_urgent_call_attempts_correlation_key", table_name="urgent_call_attempts")
    op.drop_index("uq_urgent_call_attempts_external_id", table_name="urgent_call_attempts")
    op.drop_table("urgent_call_attempts")
    op.drop_index("ix_urgent_reminder_deliveries_due", table_name="urgent_reminder_deliveries")
    op.drop_index("uq_urgent_reminder_deliveries_occurrence", table_name="urgent_reminder_deliveries")
    op.drop_table("urgent_reminder_deliveries")
    sa.Enum(name="urgent_call_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="urgent_delivery_status").drop(op.get_bind(), checkfirst=True)
    op.drop_column("reminder_drafts", "is_urgent")
    op.drop_column("reminders", "is_urgent")
