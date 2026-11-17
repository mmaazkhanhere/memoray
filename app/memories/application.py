"""Channel-neutral memory use cases."""

from __future__ import annotations

import uuid

from app.memories.service import MemoryService


class MemoryApplication:
    def __init__(self, service: MemoryService):
        self.service = service

    def create(self, user_id: uuid.UUID, content: str) -> dict:
        memory = self.service.create(user_id, content)
        return {
            "id": str(memory.id),
            "content": memory.content,
            "embedding_status": memory.embedding_status.value,
        }

    def search(self, user_id: uuid.UUID, query: str) -> list[dict]:
        return self.service.retrieve(user_id, query)
