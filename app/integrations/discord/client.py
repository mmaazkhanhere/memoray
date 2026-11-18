from __future__ import annotations

import base64
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.integrations.discord.errors import DiscordDeliveryError, DiscordOAuthError


class DiscordClient:
    """Small synchronous Discord HTTP client used by the API and workers."""

    api_base = "https://discord.com/api/v10"
    oauth_token_url = "https://discord.com/api/oauth2/token"

    def __init__(self, *, bot_token: str, opener=urlopen):
        self.bot_token = bot_token
        self.opener = opener

    def exchange_code(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        code: str,
        code_verifier: str,
    ) -> dict:
        body = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            }
        ).encode()
        request = Request(
            self.oauth_token_url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": "Basic "
                + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode(),
                "User-Agent": "Memoray/0.1",
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                raw_detail = exc.read().decode("utf-8", errors="replace")
                payload = json.loads(raw_detail)
                if isinstance(payload, dict):
                    detail = str(
                        payload.get("error_description")
                        or payload.get("error")
                        or payload.get("message")
                        or ""
                    )
            except (OSError, ValueError):
                pass
            suffix = f": {detail}" if detail else ""
            raise DiscordOAuthError(
                f"Discord authorization could not be completed (HTTP {exc.code}){suffix}"
            ) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise DiscordOAuthError("Discord authorization could not be completed") from exc
        if not isinstance(result, dict) or not result.get("access_token"):
            raise DiscordOAuthError("Discord did not return an access token")
        return result

    def current_user(self, access_token: str) -> dict:
        try:
            return self._request_json(
                "GET",
                "/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
                oauth=True,
            )
        except DiscordDeliveryError as exc:
            raise DiscordOAuthError("Discord user authorization could not be verified") from exc

    def create_dm(self, recipient_id: str) -> str:
        result = self._request_json(
            "POST",
            "/users/@me/channels",
            payload={"recipient_id": recipient_id},
        )
        channel_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(channel_id, str) or not channel_id:
            raise DiscordDeliveryError("Discord did not return a DM channel")
        return channel_id

    def send_message(self, channel_id: str, text: str) -> None:
        self._request_json("POST", f"/channels/{channel_id}/messages", payload={"content": text})

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
        oauth: bool = False,
    ) -> dict:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "Memoray/0.1",
        }
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        if not oauth:
            request_headers["Authorization"] = f"Bot {self.bot_token}"
        request_headers.update(headers or {})
        request = Request(
            f"{self.api_base}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=request_headers,
            method=method,
        )
        try:
            with self.opener(request, timeout=15) as response:
                raw = response.read()
                result = json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as exc:
            retry_after = None
            try:
                retry_after = float(exc.headers.get("Retry-After"))
            except (TypeError, ValueError):
                pass
            if exc.code == 429:
                raise DiscordDeliveryError(
                    "Discord rate limit", classification="retryable", retry_after=retry_after
                ) from exc
            if 500 <= exc.code < 600:
                raise DiscordDeliveryError("Discord server error", classification="retryable") from exc
            raise DiscordDeliveryError(
                f"Discord rejected the request with HTTP {exc.code}",
                classification="permanent",
                invalidate_identity=exc.code in {401, 403, 404},
            ) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise DiscordDeliveryError(
                "Discord network result was uncertain", classification="unknown"
            ) from exc
        if not isinstance(result, dict):
            raise DiscordDeliveryError("Discord returned an invalid response", classification="permanent")
        return result
