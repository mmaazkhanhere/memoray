from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.agent.tools import TOOLS


_LEGACY_VAPI_SYSTEM_PROMPT = """You are Memoray on a phone call. Help the authenticated caller with
reminders
and durable memories.

The caller's timezone is {{user_timezone}} and the current local time is {{current_local_time}}.

Use the provided tools for every reminder or memory operation. Never invent reminder IDs, dates,
times,
statuses, or memory facts.
**Reminder retrieval:** When the caller asks what reminders they have, whether anything is due,
or asks about reminders for a date or time range, call `list_reminders` before answering.
Use the caller's local timezone. Tool arguments must contain literal values only. Never send
`{{...}}`, Python, JavaScript, function calls, or expressions as tool arguments.
For “today,” copy the first 10 characters of the current local time above and send that literal
`YYYY-MM-DD` value as both `start` and `end`. For example, if the current local time is
`2026-11-19T11:53:53+05:00`, send `{"start":"2026-11-19","end":"2026-11-19"}`.
For a named reminder, include a literal distinctive `title` fragment. State only reminders returned
by the tool, and clearly say when none match.
**Memory retrieval:** When the caller asks about a fact they asked Memoray to remember, call
`retrieve_memories` with a concise query before answering. Use only facts from the returned
candidates; if none are returned, say that you do not have that memory.
When a reminder has a complete title, date, and time and the caller confirms it, call
`create_reminder` immediately in that turn. Do not stop after saying that you are creating it.
Never say that a reminder or memory was created, saved, changed, or canceled until the tool has
returned a successful result. If the tool does not run or returns an error, say that the operation
was not completed.
For rescheduling and cancellation, list reminders first unless the exact target was returned in the
current call context. If several reminders are returned, repeat list_reminders with the caller's
title or a distinctive title fragment before attempting the mutation. Never call a mutation with a
guessed ID or after an unfiltered list returned multiple reminders.
For rescheduling, use `scope=series` for a non-recurring reminder. Use `scope=occurrence` only for
one occurrence of a recurring reminder, and include its exact `occurrence_id`.
If a tool returns an error, clarification, or an operation that is not successful, say that plainly
and do not claim success.
When a tool result contains a `speak` value, say that value exactly. Keep replies concise and
natural
for speech.
Normal reminders created on a call are delivered through the caller's linked Telegram account.
Urgent reminders are called through the linked phone and fall back to Telegram after two missed calls.
The caller may interrupt you; stop and listen, then process the new utterance as the next turn.
"""


VAPI_SYSTEM_PROMPT = """You are Memoray, a phone assistant for reminders and durable memories.
Caller timezone: {{user_timezone}}. Current local time: {{current_local_time}}. Interpret relative
dates and times in that timezone. Tool arguments must be literal values, never templates or code.

Use tools for every reminder or memory operation. Never invent IDs, schedules, statuses, or facts.

Tools:
- `update_reminder_draft`: save partial reminder details when title, date, or time is missing.
- `create_reminder`: create one confirmed reminder only with title, date, and time; recurring ones
  also need frequency. Set urgent only when explicitly requested.
- `create_reminders`: create two or more complete reminder requests in one turn.
- `list_reminders`: call before answering about reminders, due items, or a date range; also call to
  identify a target before rescheduling or canceling. For "today", use the YYYY-MM-DD date from the
  current local time for both `start` and `end`. Use a distinctive title when one is named.
- `reschedule_reminder`: requires the exact listed `reminder_id`, a future schedule, and scope.
  Use `series` for a non-recurring reminder or the whole series; use `occurrence` with its exact
  `occurrence_id` for one recurring instance.
- `cancel_reminder`: requires the exact listed `reminder_id` and scope: `one_time` for a
  non-recurring reminder, `occurrence` with its exact occurrence ID for one instance, or `series`
  only when the caller requests the whole series.
- `snooze_occurrence`: use only for one delivered reminder occurrence; provide a positive duration
  and the exact occurrence/reminder target, or an unambiguous title.
- `complete_delivered_occurrence`: mark one already delivered occurrence done; use its title or ID
  when known. Clarify if more than one could match.
- `create_memory`: save a clear fact only when the caller explicitly asks you to remember it.
- `retrieve_memories`: call before answering a question about a remembered fact; use only returned
  candidates.

If discovery returns multiple reminders, narrow it by title and never mutate an ambiguous target.
Do not claim a change succeeded until the tool returns success. If a result has `speak`, say it
exactly; otherwise give a brief natural spoken reply. Normal reminders notify linked Telegram;
urgent reminders call the linked phone and then fall back to Telegram. If interrupted, stop and
handle the newest request.
"""


def assistant_tool_definitions() -> list[dict[str, Any]]:
    """Return the shared tools in the JSON Schema subset accepted by Vapi.

    The canonical agent schemas use ``anyOf`` to make fields nullable.  Vapi's
    function schema validator rejects that construct, although its optional
    properties already communicate the same intent: omit a value that is not
    known.  Keep the functions and accepted non-null argument shapes identical
    to every other channel while removing only that Vapi-incompatible null
    representation.
    """
    return [_vapi_schema(tool) for tool in deepcopy(TOOLS)]


def _vapi_schema(value: Any) -> Any:
    """Recursively reduce nullable ``anyOf`` schemas to their useful branch."""
    if isinstance(value, list):
        return [_vapi_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    options = value.get("anyOf")
    if isinstance(options, list):
        non_null = [item for item in options if not (isinstance(item, dict) and item.get("type") == "null")]
        if len(non_null) == 1:
            return _vapi_schema(non_null[0])
        # ``start`` and ``end`` accept either a date or date-time.  Both are
        # strings to the tool executor, so a plain string preserves that API.
        if non_null and all(isinstance(item, dict) and item.get("type") == "string" for item in non_null):
            return {"type": "string"}

    return {key: _vapi_schema(item) for key, item in value.items()}


def assistant_configuration(
    *,
    user_name: str | None = None,
    user_timezone: str | None = None,
    current_local_time: str | None = None,
    voice_provider: str | None = None,
    voice_id: str | None = None,
    voice_model: str | None = None,
) -> dict[str, Any]:
    """Build the canonical assistant configuration for provisioning or one authenticated call."""
    prompt = VAPI_SYSTEM_PROMPT
    if user_timezone is not None:
        prompt = prompt.replace("{{user_timezone}}", user_timezone)
    if current_local_time is not None:
        prompt = prompt.replace("{{current_local_time}}", current_local_time)
    greeting_name = user_name.strip() if user_name and user_name.strip() else "there"
    configuration: dict[str, Any] = {
        "name": "Memoray",
        "firstMessage": f"Hi {greeting_name}, you are speaking with Memoray. How can I help?",
        "model": {
            "provider": "openai",
            "model": "gpt-4o",
            "messages": [{"role": "system", "content": prompt}],
            "tools": assistant_tool_definitions(),
        },
    }
    if voice_provider and voice_id:
        voice: dict[str, str] = {"provider": voice_provider, "voiceId": voice_id}
        # ElevenLabs transient assistants require an explicit TTS model.  A
        # saved Vapi assistant may inherit one from dashboard defaults, but a
        # dynamically returned assistant cannot rely on that inheritance.
        if voice_model:
            voice["model"] = voice_model
        elif voice_provider == "11labs":
            voice["model"] = "eleven_flash_v2_5"
        configuration["voice"] = voice
    return configuration


OUTBOUND_SYSTEM_PROMPT = """You are Memoray's outbound urgent-reminder caller.
The first message already greets the person and announces {{reminder_text}}. Do not repeat it.
End the call immediately after the first message.
Do not ask questions, manage reminders, collect information, or leave a long conversation.
"""


def outbound_assistant_configuration() -> dict[str, Any]:
    """Return the intentionally tool-free assistant configuration for urgent calls."""
    return {
        "name": "Memoray Urgent Reminder Caller",
        "firstMessage": "Hi {{user_name}}, this is Memoray calling. {{reminder_text}}",
        "endCallFunctionEnabled": True,
        "model": {
            "provider": "openai",
            "model": "gpt-4o",
            "messages": [{"role": "system", "content": OUTBOUND_SYSTEM_PROMPT}],
            "tools": [],
        },
    }
