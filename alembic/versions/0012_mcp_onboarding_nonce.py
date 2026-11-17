"""persist MCP linking through Telegram onboarding"""

from alembic import op
import sqlalchemy as sa


revision = "0012_mcp_onboarding_nonce"
down_revision = "0011_mcp_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("channel_accounts", sa.Column("pending_mcp_nonce", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("channel_accounts", "pending_mcp_nonce")
