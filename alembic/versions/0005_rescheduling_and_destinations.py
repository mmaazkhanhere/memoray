"""add delivery destinations and rescheduling metadata

Revision ID: 0005_rescheduling_destinations
Revises: 0004_recurrence_intervals
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_rescheduling_destinations"
down_revision = "0004_recurrence_intervals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    delivery_channel = sa.Enum("in_app", "email", "sms", "webhook", name="delivery_channel")
    delivery_channel.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "delivery_destinations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "channel",
            sa.Enum(
                "in_app", "email", "sms", "webhook", name="delivery_channel", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("identity", sa.String(length=500), nullable=False),
        sa.Column("is_linked", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_valid", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_destinations_user_channel", "delivery_destinations", ["user_id", "channel"]
    )
    op.add_column(
        "reminders", sa.Column("destination_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "reminders", sa.Column("series_anchor_schedule", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "reminder_occurrences", sa.Column("series_schedule", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_reminders_destination_id",
        "reminders",
        "delivery_destinations",
        ["destination_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute("UPDATE reminders SET series_anchor_schedule = schedule WHERE series_anchor_schedule IS NULL")
    op.execute(
        "UPDATE reminder_occurrences SET series_schedule = schedule WHERE series_schedule IS NULL"
    )
    op.execute(
        """
        INSERT INTO delivery_destinations
            (id, user_id, channel, identity, is_linked, is_valid)
        SELECT id, id, 'in_app', 'user:' || id::text, TRUE, TRUE
        FROM users
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        "UPDATE reminders SET destination_id = user_id WHERE destination_id IS NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_reminders_destination_id", "reminders", type_="foreignkey")
    op.drop_column("reminder_occurrences", "series_schedule")
    op.drop_column("reminders", "series_anchor_schedule")
    op.drop_column("reminders", "destination_id")
    op.drop_index("ix_destinations_user_channel", table_name="delivery_destinations")
    op.drop_table("delivery_destinations")
    sa.Enum(name="delivery_channel").drop(op.get_bind(), checkfirst=True)
