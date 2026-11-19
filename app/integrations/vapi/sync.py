"""Explicitly synchronize the saved Memoray assistant with Vapi.

This is intentionally a command, rather than application-startup behavior: changing a
production assistant should always be an explicit operator action.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import get_settings

from .assistant import assistant_configuration


def build_payload(
    *,
    server_url: str | None = None,
    credential_id: str | None = None,
    voice_provider: str | None = None,
    voice_id: str | None = None,
    voice_model: str | None = None,
) -> dict[str, Any]:
    """Build the PATCH body without including secrets or replacing server settings by default."""
    configuration = assistant_configuration(
        voice_provider=voice_provider,
        voice_id=voice_id,
        voice_model=voice_model,
    )
    payload: dict[str, Any] = {
        "firstMessage": configuration["firstMessage"],
        "name": configuration["name"],
        "model": configuration["model"],
    }
    if "voice" in configuration:
        payload["voice"] = configuration["voice"]
    if server_url:
        server: dict[str, str] = {"url": server_url}
        if credential_id:
            server["credentialId"] = credential_id
        payload["server"] = server
    return payload


def sync_assistant(
    *,
    api_key: str,
    assistant_id: str,
    server_url: str | None = None,
    credential_id: str | None = None,
    voice_provider: str | None = None,
    voice_id: str | None = None,
    voice_model: str | None = None,
) -> dict[str, Any]:
    """PATCH the configured assistant and return Vapi's response JSON."""
    endpoint = f"https://api.vapi.ai/assistant/{assistant_id}"
    body = json.dumps(
        build_payload(
            server_url=server_url,
            credential_id=credential_id,
            voice_provider=voice_provider,
            voice_id=voice_id,
            voice_model=voice_model,
        )
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Vapi's Cloudflare edge rejects urllib's default Python client
            # signature before the request reaches the Vapi API.
            "User-Agent": "Memoray-Vapi-Sync/1.0",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS API endpoint
            response_body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Vapi assistant update failed ({exc.code}): {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Vapi assistant API: {exc.reason}") from exc

    try:
        return json.loads(response_body) if response_body else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("Vapi returned a non-JSON assistant response") from exc


def main() -> int:
    settings = get_settings()
    api_key = settings.vapi_api_key or os.getenv("VAPI_API_KEY")
    assistant_id = settings.vapi_assistant_id or os.getenv("VAPI_ASSISTANT_ID")
    if not api_key:
        print("VAPI_API_KEY is required to sync the assistant.", file=sys.stderr)
        return 2
    if not assistant_id:
        print("VAPI_ASSISTANT_ID is required to sync the assistant.", file=sys.stderr)
        return 2

    try:
        result = sync_assistant(
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

    print(f"Synchronized Vapi assistant {result.get('id', assistant_id)}")
    print(f"Name: {result.get('name', '<unchanged>')}")
    print("Memoray prompt and shared function tools are now active.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
