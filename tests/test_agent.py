from app.llm.protocol import LLMResponse, ToolCall
from app.agent.service import ReminderAgent
from app.agent.tools import LIST_REMINDERS_TOOL
from app.reminders.service import ReminderService
from app.schemas import UserCreate
from app.users.service import UserService


class FakeProvider:
    def __init__(self, response):
        self.responses = response if isinstance(response, list) else [response]
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return self.responses.pop(0)


def test_unrelated_request_is_declined_by_llm(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    provider = FakeProvider(LLMResponse(text="I can only help create or retrieve reminders."))
    result = ReminderAgent(provider, ReminderService(db), UserService(db)).handle(user.id, "What is the weather?")
    assert "create or retrieve reminders" in result
    assert len(provider.calls) == 1


def test_agent_executes_create_tool(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    provider = FakeProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="create_reminder",
                        arguments={"title": "Call", "scheduled_at": "2026-11-15T09:00:00+00:00"},
                    )
                ]
            ),
            LLMResponse(text='Absolutely! I\'ll remind you about "Call" tomorrow morning.'),
        ]
    )
    result = ReminderAgent(provider, ReminderService(db), UserService(db)).handle(user.id, "Remind me to call tomorrow")
    assert result == 'Absolutely! I\'ll remind you about "Call" tomorrow morning.'
    tool_result = provider.calls[1][0][-1]
    assert tool_result["role"] == "tool"
    assert '"operation": "created"' in tool_result["content"]


def test_list_tool_allows_null_optional_filters():
    properties = LIST_REMINDERS_TOOL["function"]["parameters"]["properties"]
    for name in ("title", "start", "end", "status"):
        assert {"type": "null"} in properties[name]["anyOf"]


def test_final_llm_response_is_limited_to_25_words(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    long_response = "word " * 40
    provider = FakeProvider(
        [
            LLMResponse(
                tool_calls=[ToolCall(name="list_reminders", arguments={})]
            ),
            LLMResponse(text=long_response),
        ]
    )
    result = ReminderAgent(provider, ReminderService(db), UserService(db)).handle(user.id, "show reminders")
    assert len(result.split()) <= 25

