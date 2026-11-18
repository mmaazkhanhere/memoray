"""Explicitly synchronize the dedicated urgent-reminder Vapi assistant."""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import get_settings
from app.integrations.vapi.assistant import outbound_assistant_configuration


def build_payload(*, server_url: str | None = None, credential_id: str | None = None, voice_provider: str | None = None, voice_id: str | None = None, voice_model: str | None = None) -> dict[str, Any]:
    configuration = outbound_assistant_configuration()
    payload: dict[str, Any] = {
        "firstMessage": configuration["firstMessage"],
        "name": configuration["name"],
        "model": configuration["model"],
        "endCallFunctionEnabled": configuration["endCallFunctionEnabled"],
    }
    if voice_provider and voice_id:
        voice: dict[str, str] = {"provider": voice_provider, "voiceId": voice_id}
        if voice_model:
            voice["model"] = voice_model
        payload["voice"] = voice
    if server_url:
        payload["server"] = {"url": server_url, **({"credentialId": credential_id} if credential_id else {})}
    return payload


def sync_outbound_assistant(*, api_key: str, assistant_id: str, server_url: str | None = None, credential_id: str | None = None, voice_provider: str | None = None, voice_id: str | None = None, voice_model: str | None = None) -> dict[str, Any]:
    request = Request(
        f"https://api.vapi.ai/assistant/{assistant_id}",
        data=json.dumps(build_payload(server_url=server_url, credential_id=credential_id, voice_provider=voice_provider, voice_id=voice_id, voice_model=voice_model)).encode("utf-8"),
        method="PATCH",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS API endpoint
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Vapi outbound assistant update failed ({exc.code}): {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Vapi assistant API: {exc.reason}") from exc
    try:
        return json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("Vapi returned a non-JSON assistant response") from exc


def main() -> int:
    settings = get_settings()
    api_key = settings.vapi_api_key or os.getenv("VAPI_API_KEY")
    assistant_id = settings.vapi_outbound_assistant_id or os.getenv("VAPI_OUTBOUND_ASSISTANT_ID")
    if not api_key or not assistant_id:
        print("VAPI_API_KEY and VAPI_OUTBOUND_ASSISTANT_ID are required.", file=sys.stderr)
        return 2
    try:
        result = sync_outbound_assistant(
            api_key=api_key,
            assistant_id=assistant_id,
            server_url=settings.vapi_server_url or os.getenv("VAPI_SERVER_URL"),
            credential_id=settings.vapi_credential_id or os.getenv("VAPI_CREDENTIAL_ID"),
            voice_provider=settings.vapi_voice_provider or os.getenv("VAPI_VOICE_PROVIDER"),
            voice_id=settings.vapi_voice_id or os.getenv("VAPI_VOICE_ID"),
            voice_model=settings.vapi_voice_model or os.getenv("VAPI_VOICE_MODEL"),
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Synchronized Vapi outbound assistant {result.get('id', assistant_id)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
