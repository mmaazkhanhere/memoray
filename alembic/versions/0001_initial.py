"""create initial reminder schema

Revision ID: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    user_status = sa.Enum("active", "pending", name="user_status", create_type=False)
    reminder_status = sa.Enum("active", "completed", "canceled", name="reminder_status", create_type=False)
    occurrence_status = sa.Enum(
        "pending", "notified", "completed", "canceled", name="occurrence_status", create_type=False
    )
    user_status.create(op.get_bind())
    reminder_status.create(op.get_bind())
    occurrence_status.create(op.get_bind())
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("status", user_status, nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "reminders",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("schedule", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_recurring", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recurrence_rule", sa.Text(), nullable=True),
        sa.Column("status", reminder_status, nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_reminders_user_status_schedule", "reminders", ["user_id", "status", "schedule"])
    op.create_table(
        "reminder_occurrences",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("reminder_id", sa.UUID(), sa.ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("schedule", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", occurrence_status, nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_occurrences_reminder_status_due", "reminder_occurrences", ["reminder_id", "status", "next_due_at"])


def downgrade() -> None:
    op.drop_index("ix_occurrences_reminder_status_due", table_name="reminder_occurrences")
    op.drop_table("reminder_occurrences")
    op.drop_index("ix_reminders_user_status_schedule", table_name="reminders")
    op.drop_table("reminders")
    op.drop_table("users")
    sa.Enum(name="occurrence_status").drop(op.get_bind())
    sa.Enum(name="reminder_status").drop(op.get_bind())
    sa.Enum(name="user_status").drop(op.get_bind())

