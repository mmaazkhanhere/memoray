import json

from app.agent.service import ReminderAgent
from app.conversations.service import ConversationService
from app.llm.protocol import LLMResponse, ToolCall
from app.memories.models import MemoryEmbeddingStatus
from app.memories.service import MemoryService
from app.reminders.service import ReminderService
from app.users.service import UserService
from app.schemas import UserCreate
from app.memories.embedding import GeminiEmbeddingProvider


class FakeEmbedding:
    model = "fake-embedding"
    dimensions = 3

    def embed(self, text):
        text = text.casefold()
        if "blue" in text or "color" in text:
            return [1.0, 0.0, 0.0]
        if "cat" in text or "pet" in text:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)

    def complete(self, messages, tools):
        return self.responses.pop(0)


class FailingEmbedding:
    model = "fake-embedding"
    dimensions = 3

    def embed(self, text):
        raise RuntimeError("provider unavailable")


def memory_service(db):
    return MemoryService(db, FakeEmbedding(), min_similarity=0.8)


def test_memory_is_indexed_and_retrieved_for_one_user(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = memory_service(db)
    memory = service.create(user.id, "My favorite color is blue.")

    assert memory.embedding_status is MemoryEmbeddingStatus.indexed
    assert memory.embedding_attempts == 1
    assert service.retrieve(user.id, "What color do I like?")[0]["content"] == memory.content


def test_memory_retrieval_is_user_scoped(db):
    first = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    second = UserService(db).create(UserCreate(name="B", timezone="UTC"))
    service = memory_service(db)
    service.create(first.id, "My favorite color is blue.")

    assert service.retrieve(second.id, "What color do I like?") == []


def test_failed_indexing_preserves_memory_and_metadata(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = MemoryService(db, FailingEmbedding())
    memory = service.create(user.id, "My favorite color is blue.")

    assert memory.embedding_status is MemoryEmbeddingStatus.failed
    assert memory.embedding_attempts == 1
    assert memory.embedding_error_code == "indexing_failed"
    assert memory.embedding_content_hash == MemoryService.content_hash(memory.content)


def test_gemini_adapter_is_normalized_and_uses_embedding_endpoint(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"embedding": {"values": [3, 4, 0]}}).encode()

    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["key"] = req.headers["X-goog-api-key"]
        return Response()

    monkeypatch.setattr("app.memories.embedding.request.urlopen", fake_urlopen)
    provider = GeminiEmbeddingProvider(api_key="secret", dimensions=3)

    assert provider.embed("hello") == [0.6, 0.8, 0.0]
    assert captured["url"].endswith("/models/gemini-embedding-001:embedContent")
    assert captured["body"]["taskType"] == "RETRIEVAL_DOCUMENT"
    assert captured["body"]["outputDimensionality"] == 3
    assert captured["key"] == "secret"


def test_agent_grounds_memory_answer_and_clarifies_multiple_matches(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = memory_service(db)
    service.create(user.id, "My favorite color is blue.")
    service.create(user.id, "The blue car belongs to me.")
    provider = FakeProvider(
        [
            LLMResponse(
                tool_calls=[ToolCall(name="retrieve_memories", arguments={"query": "blue"})]
            ),
            LLMResponse(text="The answer model must not be trusted with new facts."),
        ]
    )
    result = ReminderAgent(
        provider, ReminderService(db), UserService(db), ConversationService(db), service
    ).handle(user.id, "What do you remember about blue?")

    assert "multiple matching memories" in result.response
    assert "My favorite color is blue." in result.response
    assert "The blue car belongs to me." in result.response


def test_agent_can_create_memory_with_the_memory_tool(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = memory_service(db)
    provider = FakeProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        name="create_memory",
                        arguments={"content": "My favorite color is blue."},
                    )
                ]
            ),
            LLMResponse(text="I will remember that."),
        ]
    )

    result = ReminderAgent(
        provider, ReminderService(db), UserService(db), ConversationService(db), service
    ).handle(user.id, "Remember that my favorite color is blue.")

    assert result.response == "I will remember that."
    assert service.retrieve(user.id, "favorite color")[0]["content"] == "My favorite color is blue."
