from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memories.embedding import EmbeddingError, EmbeddingProvider
from app.memories.models import Memory, MemoryEmbeddingStatus
from app.users.service import UserService


class MemoryService:
    """Owns durable memory writes, indexing state, and user-scoped retrieval."""

    def __init__(
        self,
        db: Session,
        embedding_provider: EmbeddingProvider | None = None,
        *,
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
        candidate_limit: int = 5,
        min_similarity: float = 0.65,
    ):
        self.db = db
        self.users = UserService(db)
        self.embedding_provider = embedding_provider
        self.embedding_model = embedding_model or getattr(embedding_provider, "model", "unconfigured")
        self.embedding_dimensions = embedding_dimensions or getattr(embedding_provider, "dimensions", 1536)
        self.candidate_limit = max(1, min(candidate_limit, 20))
        self.min_similarity = min_similarity

    def create(self, user_id: uuid.UUID, content: str) -> Memory:
        self.users.get(user_id)
        normalized = content.strip() if isinstance(content, str) else ""
        if not normalized:
            raise ValueError("memory content must not be blank")
        memory = Memory(
            user_id=user_id,
            content=normalized,
            embedding_status=MemoryEmbeddingStatus.pending,
            embedding_model=self.embedding_model,
            embedding_attempts=0,
        )
        self.db.add(memory)
        self.db.commit()
        self.db.refresh(memory)
        self.index(memory.id)
        self.db.refresh(memory)
        return memory

    def index(self, memory_id: uuid.UUID) -> Memory:
        memory = self.db.scalar(select(Memory).where(Memory.id == memory_id))
        if memory is None:
            raise LookupError("memory not found")
        memory.embedding_attempts += 1
        memory.embedding_content_hash = self.content_hash(memory.content)
        memory.embedding_metadata_updated_at = datetime.now(timezone.utc)
        if self.embedding_provider is None:
            memory.embedding_status = MemoryEmbeddingStatus.pending
            memory.embedding_error_code = "provider_not_configured"
            self.db.commit()
            return memory
        try:
            vector = list(self.embedding_provider.embed(memory.content))
            if len(vector) != self.embedding_dimensions:
                raise EmbeddingError("dimension_mismatch")
        except EmbeddingError as exc:
            memory.embedding_status = MemoryEmbeddingStatus.failed
            memory.embedding_error_code = exc.code[:64]
            self.db.commit()
            return memory
        except Exception:
            memory.embedding_status = MemoryEmbeddingStatus.failed
            memory.embedding_error_code = "indexing_failed"
            self.db.commit()
            return memory

        memory.embedding = vector
        memory.embedding_status = MemoryEmbeddingStatus.indexed
        memory.embedding_model = self.embedding_provider.model
        memory.embedding_error_code = None
        memory.embedding_metadata_updated_at = datetime.now(timezone.utc)
        self.db.commit()
        return memory

    def retry_pending(self, limit: int = 100) -> int:
        records = list(
            self.db.scalars(
                select(Memory)
                .where(
                    Memory.embedding_status.in_(
                        [MemoryEmbeddingStatus.pending, MemoryEmbeddingStatus.failed]
                    )
                )
                .order_by(Memory.created_at.asc())
                .limit(max(1, limit))
            )
        )
        for memory in records:
            self.index(memory.id)
        return len(records)

    def retrieve(self, user_id: uuid.UUID, query: str) -> list[dict[str, Any]]:
        normalized = query.strip() if isinstance(query, str) else ""
        if not normalized:
            raise ValueError("memory query must not be blank")
        if self.embedding_provider is None:
            return []
        try:
            embed_query = getattr(self.embedding_provider, "embed_query", self.embedding_provider.embed)
            vector = list(embed_query(normalized))
            if len(vector) != self.embedding_dimensions:
                raise EmbeddingError("dimension_mismatch")
        except Exception:
            return []

        base_filters = (
            Memory.user_id == user_id,
            Memory.embedding_status == MemoryEmbeddingStatus.indexed,
            Memory.embedding.is_not(None),
        )
        try:
            distance = Memory.embedding.cosine_distance(vector)
            statement = (
                select(Memory, distance.label("distance"))
                .where(*base_filters)
                .order_by(distance.asc())
                .limit(self.candidate_limit)
            )
            rows = self.db.execute(statement).all()
        except (AttributeError, NotImplementedError):
            rows = self._python_candidates(user_id, vector)
        except Exception:
            # SQLite unit tests do not provide pgvector operators; production PostgreSQL does.
            rows = self._python_candidates(user_id, vector)
        candidates = []
        for memory, raw_distance in rows:
            similarity = 1.0 - float(raw_distance)
            if similarity < self.min_similarity:
                continue
            candidates.append(
                {
                    "id": str(memory.id),
                    "content": memory.content,
                    "similarity": round(similarity, 6),
                }
            )
        return candidates

    def _python_candidates(
        self, user_id: uuid.UUID, vector: Sequence[float]
    ) -> list[tuple[Memory, float]]:
        records = self.db.scalars(
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.embedding_status == MemoryEmbeddingStatus.indexed,
                Memory.embedding.is_not(None),
            )
        ).all()
        query_norm = math.sqrt(sum(value * value for value in vector))
        if query_norm == 0:
            return []
        scored: list[tuple[Memory, float]] = []
        for memory in records:
            values = memory.embedding or []
            if len(values) != len(vector):
                continue
            memory_norm = math.sqrt(sum(value * value for value in values))
            if memory_norm == 0:
                continue
            cosine = sum(a * b for a, b in zip(values, vector)) / (memory_norm * query_norm)
            scored.append((memory, 1.0 - cosine))
        scored.sort(key=lambda item: item[1])
        return scored[: self.candidate_limit]

    @staticmethod
    def content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
