from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

try:  # pgvector is required in production, while the fallback keeps SQLite unit tests usable.
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover - exercised only before optional local dependency install
    from sqlalchemy import JSON
    from sqlalchemy.types import TypeDecorator

    class Vector(TypeDecorator):
        impl = JSON
        cache_ok = True

        def __init__(self, dim: int, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.dim = dim


class MemoryEmbeddingStatus(str, enum.Enum):
    pending = "pending"
    indexed = "indexed"
    failed = "failed"


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_user_embedding_status", "user_id", "embedding_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    embedding_status: Mapped[MemoryEmbeddingStatus] = mapped_column(
        Enum(MemoryEmbeddingStatus, name="memory_embedding_status"),
        default=MemoryEmbeddingStatus.pending,
        nullable=False,
    )
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    embedding_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embedding_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_metadata_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
