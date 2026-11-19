from datetime import datetime, timedelta, timezone

from app.integrations.vapi.assistant import assistant_configuration
from app.integrations.vapi.router import (
    _assistant_error,
    _assistant_response,
    _message_response,
    _tool_results_response,
    _urgent_outcome,
)
from app.integrations.vapi.service import VapiAuthenticationError, VapiCallService
from app.integrations.vapi.sync import build_payload
from app.models import (
    ChannelIdentity,
    ChannelIdentityStatus,
    ChannelKind,
    NotificationDelivery,
    NotificationStatus,
    PhoneLink,
)
from app.schemas import UserCreate
from app.users.service import UserService


def test_vapi_assistant_configuration_is_dynamic_and_shared():
    configuration = assistant_configuration(
        user_name="Caller",
        user_timezone="Asia/Karachi",
        current_local_time="2026-11-16T12:00:00+05:00",
        voice_provider="11labs",
        voice_id="voice-1",
    )
    prompt = configuration["model"]["messages"][0]["content"]
    assert configuration["name"] == "Memoray"
    assert configuration["firstMessage"].startswith("Hi Caller,")
    assert configuration["voice"] == {
        "provider": "11labs",
        "voiceId": "voice-1",
        "model": "eleven_flash_v2_5",
    }
    assert "serverMessages" not in configuration
    assert "stopSpeakingPlan" not in configuration
    assert "Asia/Karachi" in prompt
    assert "2026-11-16T12:00:00+05:00" in prompt
    assert "call\n`create_reminder` immediately" in prompt
    assert "Never say that a reminder or memory was created" in prompt
    assert len(configuration["model"]["tools"]) == 10


def test_vapi_server_responses_use_the_correct_envelope_for_each_message_type():
    assert _assistant_error("not registered") == {"messageResponse": {"error": "not registered"}}
    assert _message_response({"results": []}) == {"messageResponse": {"results": []}}
    assert _tool_results_response(
        [{"toolCallId": "call-1", "result": '{"operation":"retrieved"}'}]
    ) == {"results": [{"toolCallId": "call-1", "result": '{"operation":"retrieved"}'}]}


def test_urgent_vapi_outcome_requires_a_human_or_explicit_connected_end_reason():
    assert _urgent_outcome({"type": "status-update", "status": "ended"}, {}) == (
        "failed",
        "missing_ended_reason",
    )
    assert _urgent_outcome({"type": "status-update", "status": "ended"}, {"answeredBy": "human"}) == (
        "answered",
        "human",
    )
    assert _urgent_outcome(
        {"type": "end-of-call-report", "endedReason": "customer-did-not-answer"}, {}
    ) == ("not_picked", "customer-did-not-answer")


def test_vapi_assistant_request_selects_saved_riley_assistant():
    class Settings:
        vapi_assistant_id = "riley-assistant-id"

    class User:
        name = "Caller"
        timezone = "Asia/Karachi"

    class Call:
        user = User()

    response = _assistant_response(Call(), Settings())
    assert response["assistantId"] == "riley-assistant-id"
    variables = response["assistantOverrides"]["variableValues"]
    assert variables["user_name"] == "Caller"
    assert variables["user_timezone"] == "Asia/Karachi"
    assert variables["current_local_time"].endswith("+05:00")


def test_vapi_sync_uses_the_same_minimal_dynamic_assistant_fields():
    payload = build_payload()
    assert "serverMessages" not in payload
    assert "stopSpeakingPlan" not in payload


def linked_user(db):
    user = UserService(db).create(UserCreate(name="Caller", timezone="UTC"))
    db.add(PhoneLink(user_id=user.id, e164_number="+14155550123", is_active=True))
    db.add(
        ChannelIdentity(
            user_id=user.id,
            channel=ChannelKind.phone,
            external_principal_id="+14155550123",
            external_destination_id="+14155550123",
            status=ChannelIdentityStatus.linked,
            is_default=True,
        )
    )
    db.add(
        ChannelIdentity(
            user_id=user.id,
            channel=ChannelKind.telegram,
            external_principal_id="telegram-user",
            external_destination_id="telegram-chat",
            status=ChannelIdentityStatus.linked,
            is_default=True,
        )
    )
    db.commit()
    return user


def test_vapi_rejects_unknown_and_missing_callers(db):
    service = VapiCallService(db)
    for number in ("", "+14155550000"):
        try:
            service.authenticate_caller("call-unknown", number)
        except VapiAuthenticationError:
            pass
        else:
            raise AssertionError("unknown caller must fail closed")


def test_vapi_tool_retry_is_idempotent_and_correction_replaces_delivery(db):
    user = linked_user(db)
    service = VapiCallService(db)
    call = service.authenticate_caller("call-1", "+14155550123")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    created = service.execute_tool(
        call,
        tool_call_id="tool-create",
        name="create_reminder",
        arguments={"title": "Call Ali", "date": tomorrow, "time": "09:00"},
    )
    repeated = service.execute_tool(
        call,
        tool_call_id="tool-create",
        name="create_reminder",
        arguments={"title": "Call Ali", "date": tomorrow, "time": "09:00"},
    )
    assert created["operation"] == "created"
    assert repeated == created
    assert db.query(NotificationDelivery).one().channel.value == "telegram"

    revised = service.execute_tool(
        call,
        tool_call_id="tool-reschedule",
        name="reschedule_reminder",
        arguments={
            "reminder_id": created["reminder"]["id"],
            "schedule": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "scope": "series",
        },
    )
    assert revised["operation"] == "rescheduled"
    deliveries = db.query(NotificationDelivery).all()
    assert len(deliveries) == 2
    assert sum(row.status == NotificationStatus.canceled for row in deliveries) == 1
    assert sum(row.status == NotificationStatus.pending for row in deliveries) == 1


def test_vapi_correction_requires_an_unambiguous_target(db):
    linked_user(db)
    service = VapiCallService(db)
    call = service.authenticate_caller("call-2", "+14155550123")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    for title in ("First", "Second"):
        service.execute_tool(
            call,
            tool_call_id=f"create-{title}",
            name="create_reminder",
            arguments={"title": title, "date": tomorrow, "time": "09:00"},
        )
    listed = service.execute_tool(
        call,
        tool_call_id="list-all",
        name="list_reminders",
        arguments={"status": "pending"},
    )
    assert listed["count"] == 2
    rejected = service.execute_tool(
        call,
        tool_call_id="ambiguous-reschedule",
        name="reschedule_reminder",
        arguments={
            "reminder_id": "00000000-0000-0000-0000-000000000001",
            "schedule": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
            "scope": "series",
        },
    )
    assert rejected["operation"] == "discovery_required"
