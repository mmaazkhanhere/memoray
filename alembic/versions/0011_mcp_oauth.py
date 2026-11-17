"""add MCP OAuth account linking and mutation receipts"""

from alembic import op
import sqlalchemy as sa


revision = "0011_mcp_oauth"
down_revision = "0010_google_calendar_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_link_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("nonce_hash", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=500), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2000), nullable=False),
        sa.Column("state", sa.String(length=2000), nullable=False),
        sa.Column("code_challenge", sa.String(length=200), nullable=False),
        sa.Column("scope", sa.String(length=500), nullable=False),
        sa.Column("resource", sa.String(length=2000), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("code_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_mcp_link_attempts_nonce_hash", "mcp_link_attempts", ["nonce_hash"], unique=True)

    for table, columns in (
        ("mcp_authorization_codes", [
            sa.Column("id", sa.UUID(), nullable=False), sa.Column("code_hash", sa.String(64), nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False), sa.Column("client_id", sa.String(500), nullable=False),
            sa.Column("redirect_uri", sa.String(2000), nullable=False), sa.Column("code_challenge", sa.String(200), nullable=False),
            sa.Column("scope", sa.String(500), nullable=False), sa.Column("resource", sa.String(2000), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        ]),
        ("mcp_refresh_tokens", [
            sa.Column("id", sa.UUID(), nullable=False), sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False), sa.Column("client_id", sa.String(500), nullable=False),
            sa.Column("scope", sa.String(500), nullable=False), sa.Column("resource", sa.String(2000), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        ]),
        ("mcp_operation_receipts", [
            sa.Column("id", sa.UUID(), nullable=False), sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("client_id", sa.String(500), nullable=False), sa.Column("operation_key", sa.String(200), nullable=False),
            sa.Column("arguments_hash", sa.String(64), nullable=False), sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        ]),
    ):
        constraints = [sa.PrimaryKeyConstraint("id")]
        if table != "mcp_link_attempts":
            constraints.append(sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"))
        op.create_table(table, *columns, *constraints)
        unique_column = "code_hash" if table == "mcp_authorization_codes" else "token_hash" if table == "mcp_refresh_tokens" else None
        if unique_column:
            op.create_index(f"uq_{table}_{unique_column}", table, [unique_column], unique=True)
    op.create_index(
        "uq_mcp_operation_receipts_user_key", "mcp_operation_receipts",
        ["user_id", "client_id", "operation_key"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_mcp_operation_receipts_user_key", table_name="mcp_operation_receipts")
    op.drop_index("uq_mcp_refresh_tokens_token_hash", table_name="mcp_refresh_tokens")
    op.drop_index("uq_mcp_authorization_codes_code_hash", table_name="mcp_authorization_codes")
    op.drop_table("mcp_operation_receipts")
    op.drop_table("mcp_refresh_tokens")
    op.drop_table("mcp_authorization_codes")
    op.drop_index("uq_mcp_link_attempts_nonce_hash", table_name="mcp_link_attempts")
    op.drop_table("mcp_link_attempts")
