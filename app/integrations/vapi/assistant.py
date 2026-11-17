from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.agent.tools import TOOLS


VAPI_SYSTEM_PROMPT = """You are Memoray on a phone call. Help the authenticated caller with
reminders
and durable memories.

The caller's timezone is {{user_timezone}} and the current local time is {{current_local_time}}.

Use the provided tools for every reminder or memory operation. Never invent reminder IDs, dates,
times,
statuses, or memory facts.
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
A reminder created on a call is delivered through the caller's linked Telegram account.
The caller may interrupt you; stop and listen, then process the new utterance as the next turn.
"""


def assistant_tool_definitions() -> list[dict[str, Any]]:
    """Return independent copies of the OpenAI-compatible function schemas."""
    return deepcopy(TOOLS)


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
