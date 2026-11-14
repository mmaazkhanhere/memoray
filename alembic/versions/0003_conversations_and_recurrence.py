"""add conversational drafts and constrained recurrence

Revision ID: 0003_conversations_recurrence
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_conversations_recurrence"
down_revision = "0002_auth_and_duplicate_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    recurrence_frequency = sa.Enum(
        "daily", "weekly", "monthly", "yearly", name="recurrence_frequency", create_type=False
    )
    conversation_status = sa.Enum(
        "active", "completed", "canceled", name="conversation_status", create_type=False
    )
    message_role = sa.Enum("user", "assistant", name="conversation_message_role", create_type=False)
    draft_status = sa.Enum("collecting", "ready", "created", "canceled", name="draft_status", create_type=False)
    recurrence_frequency.create(bind)
    conversation_status.create(bind)
    message_role.create(bind)
    draft_status.create(bind)

    op.add_column("reminders", sa.Column("recurrence_frequency", recurrence_frequency, nullable=True))
    op.create_table(
        "reminder_conversations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", conversation_status, nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_conversations_user_status_updated",
        "reminder_conversations",
        ["user_id", "status", "updated_at"],
    )
    op.create_table(
        "reminder_conversation_messages",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.UUID(),
            sa.ForeignKey("reminder_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", message_role, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_conversation_messages_conversation_created",
        "reminder_conversation_messages",
        ["conversation_id", "created_at"],
    )
    op.create_table(
        "reminder_drafts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.UUID(),
            sa.ForeignKey("reminder_conversations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("schedule_date", sa.Date(), nullable=True),
        sa.Column("schedule_time", sa.Time(), nullable=True),
        sa.Column("is_recurring", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recurrence_frequency", recurrence_frequency, nullable=True),
        sa.Column("status", draft_status, nullable=False, server_default="collecting"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_drafts_conversation_status", "reminder_drafts", ["conversation_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_drafts_conversation_status", table_name="reminder_drafts")
    op.drop_table("reminder_drafts")
    op.drop_index("ix_conversation_messages_conversation_created", table_name="reminder_conversation_messages")
    op.drop_table("reminder_conversation_messages")
    op.drop_index("ix_conversations_user_status_updated", table_name="reminder_conversations")
    op.drop_table("reminder_conversations")
    op.drop_column("reminders", "recurrence_frequency")
    bind = op.get_bind()
    sa.Enum(name="draft_status").drop(bind)
    sa.Enum(name="conversation_message_role").drop(bind)
    sa.Enum(name="conversation_status").drop(bind)
    sa.Enum(name="recurrence_frequency").drop(bind)

