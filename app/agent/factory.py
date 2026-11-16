from sqlalchemy.orm import Session

from app.agent.service import ReminderAgent
from app.config import get_settings
from app.conversations.service import ConversationService
from app.llm.groq_provider import GroqProvider
from app.memories.embedding import create_embedding_provider
from app.memories.service import MemoryService
from app.reminders.service import ReminderService
from app.users.service import UserService


def create_agent(db: Session) -> ReminderAgent:
    settings = get_settings()
    embedding_provider = create_embedding_provider(settings)
    return ReminderAgent(
        GroqProvider(settings),
        ReminderService(db),
        UserService(db),
        ConversationService(db),
        MemoryService(
            db,
            embedding_provider,
            embedding_model=settings.embedding_model,
            embedding_dimensions=settings.embedding_dimensions,
            candidate_limit=settings.memory_candidate_limit,
            min_similarity=settings.memory_min_similarity,
        ),
    )
