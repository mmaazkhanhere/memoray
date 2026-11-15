from datetime import datetime, timedelta, timezone

from app.llm.protocol import LLMResponse, ToolCall
from app.agent.service import ReminderAgent
from app.agent.tools import LIST_REMINDERS_TOOL, RESCHEDULE_REMINDER_TOOL
from app.conversations.service import ConversationService
from app.reminders.service import ReminderService
from app.schemas import ReminderCreate, ReminderQuery, UserCreate
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
    result = ReminderAgent(provider, ReminderService(db), UserService(db), ConversationService(db)).handle(
        user.id, "What is the weather?"
    )
    assert "create or retrieve reminders" in result.response
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
                        arguments={"title": "Call", "date": "2026-11-15", "time": "09:00:00"},
                    )
                ]
            ),
            LLMResponse(text='Absolutely! I\'ll remind you about "Call" tomorrow morning.'),
        ]
    )
    result = ReminderAgent(provider, ReminderService(db), UserService(db), ConversationService(db)).handle(
        user.id, "Remind me to call tomorrow"
    )
    assert result.response == 'Absolutely! I\'ll remind you about "Call" tomorrow morning.'
    tool_result = provider.calls[1][0][-1]
    assert tool_result["role"] == "user"
    assert '"operation": "created"' in tool_result["content"]


def test_agent_discovers_then_reschedules_with_exact_id(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    reminder = ReminderService(db).create(
        user.id,
        ReminderCreate(
            title="Call Ali",
            schedule=datetime.now(timezone.utc) + timedelta(days=1),
        ),
    )
    provider = FakeProvider(
        [
            LLMResponse(tool_calls=[ToolCall(name="list_reminders", arguments={"title": "Call Ali", "status": "pending"})]),
            LLMResponse(tool_calls=[ToolCall(name="reschedule_reminder", arguments={
                "reminder_id": str(reminder.id),
                "schedule": "2026-11-16T20:00:00+00:00",
                "scope": "series",
            })]),
            LLMResponse(text="Moved your reminder."),
        ]
    )
    result = ReminderAgent(provider, ReminderService(db), UserService(db), ConversationService(db)).handle(
        user.id, "Move my Call Ali reminder to Monday at 8 PM"
    )
    refreshed = ReminderService(db).list(user.id, ReminderQuery(title="Call Ali"))[0]
    assert result.response == "Moved your reminder."
    assert refreshed.schedule.hour == 20
    assert len(provider.calls) == 3


def test_agent_uses_one_filtered_and_one_broad_discovery_before_rescheduling(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    reminder = ReminderService(db).create(
        user.id,
        ReminderCreate(
            title="Take medicine",
            schedule=datetime.now(timezone.utc) + timedelta(days=1),
        ),
    )
    provider = FakeProvider(
        [
            LLMResponse(tool_calls=[ToolCall(name="list_reminders", arguments={"title": "medicine", "status": "pending"})]),
            LLMResponse(tool_calls=[ToolCall(name="list_reminders", arguments={"status": "pending"})]),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        name="reschedule_reminder",
                        arguments={
                            "reminder_id": str(reminder.id),
                            "schedule": "2026-11-16T17:00:00+00:00",
                            "scope": "series",
                        },
                    )
                ]
            ),
            LLMResponse(text="Moved your medicine reminder."),
        ]
    )

    result = ReminderAgent(
        provider, ReminderService(db), UserService(db), ConversationService(db)
    ).handle(user.id, "Move my medicine reminder to 5 PM")

    refreshed = ReminderService(db).list(user.id, ReminderQuery(title="Take medicine"))[0]
    assert result.response == "Moved your medicine reminder."
    assert refreshed.schedule.hour == 17
    assert len(provider.calls) == 4


def test_agent_can_follow_list_with_mutation_before_final_response(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    reminder = ReminderService(db).create(
        user.id,
        ReminderCreate(
            title="Gym workout",
            schedule=datetime.now(timezone.utc) + timedelta(days=1),
        ),
    )
    provider = FakeProvider(
        [
            LLMResponse(tool_calls=[ToolCall(name="list_reminders", arguments={"title": "Gym"})]),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        name="reschedule_reminder",
                        arguments={
                            "reminder_id": str(reminder.id),
                            "schedule": "2026-11-16T09:00:00+00:00",
                            "scope": "series",
                        },
                    )
                ]
            ),
            LLMResponse(text="Moved your gym reminder."),
        ]
    )
    result = ReminderAgent(provider, ReminderService(db), UserService(db), ConversationService(db)).handle(
        user.id, "Move my gym reminder to Monday at 9 AM"
    )
    refreshed = ReminderService(db).list(user.id, ReminderQuery(title="Gym workout"))[0]
    assert result.response == "Moved your gym reminder."
    assert refreshed.schedule.hour == 9
    assert len(provider.calls) == 3


def test_list_tool_allows_null_optional_filters():
    properties = LIST_REMINDERS_TOOL["function"]["parameters"]["properties"]
    for name in ("title", "start", "end", "occurrence_start", "occurrence_end", "status"):
        assert {"type": "null"} in properties[name]["anyOf"]
    assert {"type": "string", "format": "date"} in properties["start"]["anyOf"]


def test_reschedule_tool_is_mutation_only():
    properties = RESCHEDULE_REMINDER_TOOL["function"]["parameters"]["properties"]
    assert RESCHEDULE_REMINDER_TOOL["function"]["parameters"]["required"] == [
        "reminder_id", "schedule", "scope"
    ]
    assert "title" not in properties
    assert "date" not in properties
    assert "time" not in properties


def test_reschedule_requires_an_exact_id_and_does_not_search(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    agent = ReminderAgent(FakeProvider(LLMResponse(text="ok")), ReminderService(db), UserService(db), ConversationService(db))
    result = agent._reschedule(
        user.id,
        {
            "title": "Call Ali",
            "schedule": "2026-11-16T20:00:00+00:00",
            "scope": "series",
        },
    )
    assert result["operation"] == "discovery_required"


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
    result = ReminderAgent(provider, ReminderService(db), UserService(db), ConversationService(db)).handle(
        user.id, "show reminders"
    )
    assert len(result.response.split()) <= 25


def test_creation_can_collect_details_across_turns(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    provider = FakeProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        name="update_reminder_draft",
                        arguments={"title": "Call Ali", "date": "2026-11-15"},
                    )
                ]
            ),
            LLMResponse(text="What time should I use?"),
            LLMResponse(
                tool_calls=[
                    ToolCall(name="update_reminder_draft", arguments={"time": "09:00:00"})
                ]
            ),
            LLMResponse(text="Done — I will remind you tomorrow at 9 AM."),
        ]
    )
    agent = ReminderAgent(provider, ReminderService(db), UserService(db), ConversationService(db))
    first = agent.handle(user.id, "Remind me to call Ali tomorrow")
    second = agent.handle(user.id, "At 9 AM", first.conversation_id)
    assert first.draft_status == "collecting"
    assert second.draft_status == "created"
    assert "9 AM" in second.response


def test_completed_conversation_reuses_its_draft(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    conversations = ConversationService(db)
    conversation = conversations.get_or_create(user.id, None)
    conversations.mark_created(conversation.draft, conversation)
    resumed = conversations.get_or_create(user.id, conversation.id)
    assert resumed.draft.id == conversation.draft.id
    assert resumed.draft.status.value == "collecting"


def test_one_conversation_can_create_multiple_reminders_and_list_them(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    provider = FakeProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        name="create_reminder",
                        arguments={"title": "First", "date": "2026-11-15", "time": "09:00"},
                    )
                ]
            ),
            LLMResponse(text="Created the first reminder."),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        name="create_reminder",
                        arguments={"title": "Second", "date": "2026-11-16", "time": "10:00"},
                    )
                ]
            ),
            LLMResponse(text="Created the second reminder."),
            LLMResponse(tool_calls=[ToolCall(name="list_reminders", arguments={})]),
            LLMResponse(text="You have two reminders."),
        ]
    )
    agent = ReminderAgent(provider, ReminderService(db), UserService(db), ConversationService(db))
    first = agent.handle(user.id, "Remind me to do the first task tomorrow at 9 AM")
    second = agent.handle(user.id, "Also remind me about the second task the next day at 10 AM", first.conversation_id)
    listed = agent.handle(user.id, "What reminders do I have?", second.conversation_id)
    assert first.draft_status == "created"
    assert second.draft_status == "created"
    assert listed.response == "You have two reminders."
    assert len(ReminderService(db).list(user.id, ReminderQuery())) == 2


def test_one_message_can_create_multiple_reminders(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    provider = FakeProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        name="create_reminder",
                        arguments={"title": "First", "date": "2026-11-15", "time": "09:00"},
                    ),
                    ToolCall(
                        name="create_reminder",
                        arguments={"title": "Second", "date": "2026-11-16", "time": "10:00"},
                    ),
                ]
            ),
            LLMResponse(text="Created both reminders."),
        ]
    )
    agent = ReminderAgent(provider, ReminderService(db), UserService(db), ConversationService(db))
    result = agent.handle(user.id, "Create two reminders for my tasks")
    assert result.response == "Created both reminders."
    assert len(ReminderService(db).list(user.id, ReminderQuery())) == 2


def test_batch_tool_creates_each_reminder_item(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    provider = FakeProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        name="create_reminders",
                        arguments={
                            "reminders": [
                                {"title": "First", "date": "2026-11-15", "time": "09:00"},
                                {"title": "Second", "date": "2026-11-16", "time": "10:00"},
                            ]
                        },
                    )
                ]
            ),
            LLMResponse(text="Created both reminders."),
        ]
    )
    agent = ReminderAgent(provider, ReminderService(db), UserService(db), ConversationService(db))
    result = agent.handle(user.id, "Create two reminders")
    assert result.response == "Created both reminders."
    assert len(ReminderService(db).list(user.id, ReminderQuery())) == 2

