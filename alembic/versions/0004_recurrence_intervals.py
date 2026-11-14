"""add recurrence intervals

Revision ID: 0004_recurrence_intervals
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_recurrence_intervals"
down_revision = "0003_conversations_recurrence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reminders", sa.Column("recurrence_interval", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column(
        "reminder_drafts", sa.Column("recurrence_interval", sa.Integer(), nullable=False, server_default="1")
    )


def downgrade() -> None:
    op.drop_column("reminder_drafts", "recurrence_interval")
    op.drop_column("reminders", "recurrence_interval")

