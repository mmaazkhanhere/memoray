from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class VapiOutboundError(RuntimeError):
    pass


class VapiOutboundClient:
    """Small provider adapter for starting an outbound Vapi call."""

    def __init__(self, *, api_key: str, assistant_id: str, phone_number_id: str, opener=urlopen):
        self.api_key = api_key
        self.assistant_id = assistant_id
        self.phone_number_id = phone_number_id
        self.opener = opener

    def start_call(self, *, phone_number: str, correlation_key: str, user_name: str, reminder_text: str) -> str:
        payload = {
            "assistantId": self.assistant_id,
            "phoneNumberId": self.phone_number_id,
            "customer": {"number": phone_number},
            "assistantOverrides": {
                "variableValues": {
                    "user_name": user_name or "there",
                    "reminder_text": reminder_text,
                }
            },
            "metadata": {"urgent_attempt_key": correlation_key},
        }
        request = Request(
            "https://api.vapi.ai/call",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Idempotency-Key": correlation_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
            },
        )
        try:
            with self.opener(request, timeout=30) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise VapiOutboundError(f"Vapi outbound call failed ({exc.code}): {detail[:500]}") from exc
        except URLError as exc:
            raise VapiOutboundError(f"Could not reach Vapi outbound call API: {exc.reason}") from exc
        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise VapiOutboundError("Vapi returned a non-JSON call response") from exc
        call_id = result.get("id") if isinstance(result, dict) else None
        if not call_id:
            raise VapiOutboundError("Vapi outbound call response did not include an ID")
        return str(call_id)
