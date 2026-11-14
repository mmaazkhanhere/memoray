"""add user auth tokens and active reminder duplicate guard

Revision ID: 0002_auth_and_duplicate_guard
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_auth_and_duplicate_guard"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("auth_token_hash", sa.String(length=128), nullable=True))
    op.create_index("ix_users_auth_token_hash", "users", ["auth_token_hash"], unique=True)
    op.add_column("reminders", sa.Column("normalized_title", sa.String(length=500), nullable=True))
    op.execute("UPDATE reminders SET normalized_title = lower(trim(title))")
    op.alter_column("reminders", "normalized_title", nullable=False)
    op.create_index(
        "uq_active_reminders_user_title_schedule",
        "reminders",
        ["user_id", "normalized_title", "schedule"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_active_reminders_user_title_schedule", table_name="reminders")
    op.drop_column("reminders", "normalized_title")
    op.drop_index("ix_users_auth_token_hash", table_name="users")
    op.drop_column("users", "auth_token_hash")

