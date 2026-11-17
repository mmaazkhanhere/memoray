"""add authenticated Vapi call state and tool result idempotency"""

from alembic import op
import sqlalchemy as sa


revision = "0009_vapi_calls"
down_revision = "0008_durable_notification_worker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    vapi_call_status = sa.Enum("active", "ended", name="vapi_call_status")
    vapi_call_status.create(bind, checkfirst=True)

    op.create_table(
        "vapi_calls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("external_call_id", sa.String(length=200), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("caller_e164", sa.String(length=32), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "ended", name="vapi_call_status", create_type=False),
            nullable=False,
        ),
        sa.Column("last_reminder_id", sa.UUID(), nullable=True),
        sa.Column("last_occurrence_id", sa.UUID(), nullable=True),
        sa.Column("target_ambiguous", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["reminder_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["last_reminder_id"], ["reminders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["last_occurrence_id"], ["reminder_occurrences.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_vapi_calls_external_id", "vapi_calls", ["external_call_id"], unique=True)
    op.create_index("ix_vapi_calls_user_status", "vapi_calls", ["user_id", "status"])

    op.create_table(
        "vapi_tool_executions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("vapi_call_id", sa.UUID(), nullable=False),
        sa.Column("external_tool_call_id", sa.String(length=200), nullable=False),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("arguments_hash", sa.String(length=64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["vapi_call_id"], ["vapi_calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_vapi_tool_executions_call_tool",
        "vapi_tool_executions",
        ["vapi_call_id", "external_tool_call_id"],
        unique=True,
    )
    op.create_index(
        "ix_vapi_tool_executions_call_created",
        "vapi_tool_executions",
        ["vapi_call_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_vapi_tool_executions_call_created", table_name="vapi_tool_executions")
    op.drop_index("uq_vapi_tool_executions_call_tool", table_name="vapi_tool_executions")
    op.drop_table("vapi_tool_executions")
    op.drop_index("ix_vapi_calls_user_status", table_name="vapi_calls")
    op.drop_index("uq_vapi_calls_external_id", table_name="vapi_calls")
    op.drop_table("vapi_calls")
    sa.Enum(name="vapi_call_status").drop(op.get_bind(), checkfirst=True)
