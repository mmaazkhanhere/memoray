from datetime import datetime, timedelta, timezone

import pytest

from app.agent.service import ReminderAgent
from app.conversations.service import ConversationService
from app.llm.protocol import LLMResponse, ToolCall
from app.models import OccurrenceStatus, RecurrenceFrequency, ReminderStatus
from app.reminders.errors import OccurrenceNotCancelableError
from app.reminders.service import ReminderService
from app.schemas import (
    CancellationScope,
    ReminderCancelRequest,
    ReminderCreate,
    ReminderQuery,
    UserCreate,
)
from app.users.service import UserService


def future(days=1):
    return datetime.now(timezone.utc) + timedelta(days=days)


def test_cancel_one_time_marks_reminder_and_occurrence_canceled(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    reminder = service.create(user.id, ReminderCreate(title="Call", schedule=future()))

    canceled = service.cancel(
        user.id,
        reminder.id,
        ReminderCancelRequest(scope=CancellationScope.one_time),
    )

    assert canceled.status == ReminderStatus.canceled
    assert canceled.occurrences[0].status == OccurrenceStatus.canceled
    assert service.list(user.id, ReminderQuery()) == [canceled]


def test_cancel_one_recurring_occurrence_keeps_series_active_and_advances(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    reminder = service.create(
        user.id,
        ReminderCreate(
            title="Medicine",
            schedule=future(),
            is_recurring=True,
            recurrence_frequency=RecurrenceFrequency.daily,
        ),
    )
    occurrence = reminder.occurrences[0]

    canceled = service.cancel(
        user.id,
        reminder.id,
        ReminderCancelRequest(
            scope=CancellationScope.occurrence,
            occurrence_id=occurrence.id,
        ),
    )

    assert canceled.status == ReminderStatus.active
    assert canceled.recurrence_frequency == RecurrenceFrequency.daily
    assert occurrence.status == OccurrenceStatus.canceled
    pending = [item for item in canceled.occurrences if item.status == OccurrenceStatus.pending]
    assert len(pending) == 1
    assert pending[0].schedule > occurrence.schedule


def test_cancel_recurring_series_disables_all_future_delivery(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    reminder = service.create(
        user.id,
        ReminderCreate(
            title="Standup",
            schedule=future(),
            is_recurring=True,
            recurrence_frequency=RecurrenceFrequency.daily,
        ),
    )

    canceled = service.cancel(
        user.id,
        reminder.id,
        ReminderCancelRequest(scope=CancellationScope.series),
    )

    assert canceled.status == ReminderStatus.canceled
    assert all(item.status == OccurrenceStatus.canceled for item in canceled.occurrences)


def test_cancel_rejects_non_pending_occurrence(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    reminder = service.create(
        user.id,
        ReminderCreate(
            title="Standup",
            schedule=future(),
            is_recurring=True,
            recurrence_frequency=RecurrenceFrequency.daily,
        ),
    )
    reminder.occurrences[0].status = OccurrenceStatus.completed
    db.commit()

    with pytest.raises(OccurrenceNotCancelableError):
        service.cancel(
            user.id,
            reminder.id,
            ReminderCancelRequest(
                scope=CancellationScope.occurrence,
                occurrence_id=reminder.occurrences[0].id,
            ),
        )


def test_agent_discovers_then_cancels_exact_reminder(db):
    user = UserService(db).create(UserCreate(name="A", timezone="UTC"))
    service = ReminderService(db)
    reminder = service.create(user.id, ReminderCreate(title="Call Ali", schedule=future()))
    provider = FakeProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        name="list_reminders",
                        arguments={"title": "Call Ali", "status": "pending"},
                    )
                ]
            ),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        name="cancel_reminder",
                        arguments={"reminder_id": str(reminder.id), "scope": "one_time"},
                    )
                ]
            ),
            LLMResponse(text="Canceled your Call Ali reminder."),
        ]
    )

    result = ReminderAgent(provider, service, UserService(db), ConversationService(db)).handle(
        user.id, "Cancel my Call Ali reminder"
    )

    assert result.response == "Canceled your Call Ali reminder."
    assert service.db.get(type(reminder), reminder.id).status == ReminderStatus.canceled
    assert len(provider.calls) == 3


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return self.responses.pop(0)
