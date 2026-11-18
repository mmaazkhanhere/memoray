"""add Discord channel support and account-link OAuth state"""

from alembic import op
import sqlalchemy as sa


revision = "0013_discord_channel"
down_revision = "0012_mcp_onboarding_nonce"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # These enums were created separately as the schema evolved. PostgreSQL
    # cannot remove enum values safely, so the downgrade intentionally leaves
    # the added values in place just like previous channel migrations.
    if bind.dialect.name == "postgresql":
        for enum_name in (
            "channel_kind",
            "inbound_channel_kind",
            "outbound_channel_kind",
            "identity_channel_kind",
            "delivery_channel",
        ):
            op.execute(sa.text(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS 'discord'"))

    op.create_table(
        "discord_link_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("code_verifier", sa.String(length=200), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_discord_link_attempts_state_hash",
        "discord_link_attempts",
        ["state_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_discord_link_attempts_state_hash", table_name="discord_link_attempts")
    op.drop_table("discord_link_attempts")
