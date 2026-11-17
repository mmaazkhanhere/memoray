"""add reminder projection outbox and optional Google Calendar connection"""

from alembic import op
import sqlalchemy as sa


revision = "0010_google_calendar_projection"
down_revision = "0009_vapi_calls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    change_status = sa.Enum(
        "pending", "sending", "completed", "failed", name="reminder_change_status"
    )
    change_status.create(bind, checkfirst=True)
    calendar_status = sa.Enum(
        "active", "reauthorization_required", "revoked", name="google_calendar_connection_status"
    )
    calendar_status.create(bind, checkfirst=True)

    op.create_table(
        "reminder_changes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("occurrence_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sending",
                "completed",
                "failed",
                name="reminder_change_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("worker_id", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["occurrence_id"], ["reminder_occurrences.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reminder_changes_due", "reminder_changes", ["status", "next_attempt_at"])
    op.create_index(
        "ix_reminder_changes_occurrence", "reminder_changes", ["occurrence_id", "status"]
    )

    op.create_table(
        "google_calendar_connections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("calendar_id", sa.String(length=500), nullable=False, server_default="primary"),
        sa.Column("google_subject", sa.String(length=255), nullable=True),
        sa.Column("google_email", sa.String(length=320), nullable=True),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "reauthorization_required",
                "revoked",
                name="google_calendar_connection_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("connected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_google_calendar_connections_user"),
    )

    op.create_table(
        "google_calendar_oauth_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("code_verifier", sa.String(length=200), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash", name="uq_google_calendar_oauth_attempts_state"),
    )

    op.create_table(
        "google_calendar_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("occurrence_id", sa.UUID(), nullable=False),
        sa.Column("external_event_id", sa.String(length=1024), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["google_calendar_connections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["occurrence_id"], ["reminder_occurrences.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id", "occurrence_id", name="uq_google_calendar_events_connection_occurrence"
        ),
        sa.UniqueConstraint(
            "connection_id",
            "external_event_id",
            name="uq_google_calendar_events_connection_external",
        ),
    )


def downgrade() -> None:
    op.drop_table("google_calendar_events")
    op.drop_table("google_calendar_oauth_attempts")
    op.drop_table("google_calendar_connections")
    op.drop_index("ix_reminder_changes_occurrence", table_name="reminder_changes")
    op.drop_index("ix_reminder_changes_due", table_name="reminder_changes")
    op.drop_table("reminder_changes")
    sa.Enum(name="google_calendar_connection_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="reminder_change_status").drop(op.get_bind(), checkfirst=True)
