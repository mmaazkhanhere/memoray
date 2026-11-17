from sqlalchemy.orm import Session

from app.agent.service import ReminderAgent
from app.config import get_settings
from app.conversations.service import ConversationService
from app.llm.groq_provider import GroqProvider
from app.memories.embedding import create_embedding_provider
from app.memories.service import MemoryService
from app.reminders.service import ReminderService
from app.users.service import UserService


class _ToolOnlyProvider:
    """Marker provider for channel adapters that execute tools locally."""

    def complete(self, messages, tools):  # pragma: no cover - defensive only
        raise RuntimeError("tool-only agent cannot complete an LLM request")


def _memory_service(db: Session, settings):
    embedding_provider = create_embedding_provider(settings)
    return MemoryService(
        db,
        embedding_provider,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        candidate_limit=settings.memory_candidate_limit,
        min_similarity=settings.memory_min_similarity,
    )


def create_agent(db: Session) -> ReminderAgent:
    settings = get_settings()
    return ReminderAgent(
        GroqProvider(settings),
        ReminderService(db),
        UserService(db),
        ConversationService(db),
        _memory_service(db, settings),
    )


def create_tool_agent(db: Session, *, notification_identity=None) -> ReminderAgent:
    """Build the shared deterministic tool executor without requiring Groq."""
    settings = get_settings()
    return ReminderAgent(
        _ToolOnlyProvider(),
        ReminderService(db),
        UserService(db),
        ConversationService(db),
        _memory_service(db, settings),
        notification_identity=notification_identity,
    )
