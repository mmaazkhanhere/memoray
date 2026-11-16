"""add durable memories and pgvector indexing metadata

Revision ID: 0006_memories
Revises: 0005_rescheduling_destinations
"""

from alembic import op
import sqlalchemy as sa

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover - migration requires pgvector in deployments
    Vector = None


revision = "0006_memories"
down_revision = "0005_rescheduling_destinations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    embedding_status = sa.Enum(
        "pending", "indexed", "failed", name="memory_embedding_status"
    )
    embedding_status.create(op.get_bind(), checkfirst=True)
    vector_type = Vector(1536) if Vector is not None else sa.Text()
    op.create_table(
        "memories",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("embedding", vector_type, nullable=True),
        sa.Column(
            "embedding_status",
            sa.Enum("pending", "indexed", "failed", name="memory_embedding_status", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("embedding_model", sa.String(length=200), nullable=False),
        sa.Column("embedding_content_hash", sa.String(length=64), nullable=True),
        sa.Column("embedding_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding_error_code", sa.String(length=64), nullable=True),
        sa.Column("embedding_metadata_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memories_user_embedding_status",
        "memories",
        ["user_id", "embedding_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_memories_user_embedding_status", table_name="memories")
    op.drop_table("memories")
    sa.Enum(name="memory_embedding_status").drop(op.get_bind(), checkfirst=True)
