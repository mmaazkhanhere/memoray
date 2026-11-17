from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from app.config import Settings, get_settings
from app.integrations.google_calendar.errors import GoogleCalendarApiError
from app.integrations.google_calendar.oauth import TOKEN_ENDPOINT
from app.models import GoogleCalendarConnection


class GoogleCalendarClient:
    def __init__(self, *, settings: Settings | None = None, opener=urlopen):
        self.settings = settings or get_settings()
        self.opener = opener

    def exchange_code(self, code: str, verifier: str) -> dict:
        payload = self._request_json(
            TOKEN_ENDPOINT,
            method="POST",
            form={
                "code": code,
                "client_id": self.settings.google_calendar_client_id,
                "client_secret": self.settings.google_calendar_client_secret,
                "redirect_uri": self.settings.google_calendar_redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        )
        return payload

    def refresh_access_token(
        self, connection: GoogleCalendarConnection, refresh_token: str
    ) -> tuple[str, datetime | None]:
        payload = self._request_json(
            TOKEN_ENDPOINT,
            method="POST",
            form={
                "client_id": self.settings.google_calendar_client_id,
                "client_secret": self.settings.google_calendar_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise GoogleCalendarApiError("Google did not return an access token")
        expires_in = payload.get("expires_in")
        expires_at = None
        if isinstance(expires_in, (int, float)):
            expires_at = datetime.now(timezone.utc).replace(microsecond=0)
            from datetime import timedelta

            expires_at += timedelta(seconds=int(expires_in))
        return access_token, expires_at

    def upsert_event(self, calendar_id: str, event_id: str, body: dict, access_token: str) -> None:
        url = self._event_url(calendar_id, event_id)
        try:
            self._request_json(
                url,
                method="PUT",
                json_body={**body, "id": event_id},
                access_token=access_token,
            )
        except GoogleCalendarApiError as exc:
            if exc.status_code != 404:
                raise
            self._request_json(
                (
                    "https://www.googleapis.com/calendar/v3/calendars/"
                    f"{quote(calendar_id, safe='')}/events"
                ),
                method="POST",
                json_body={**body, "id": event_id},
                access_token=access_token,
            )

    def delete_event(self, calendar_id: str, event_id: str, access_token: str) -> None:
        try:
            self._request_json(
                self._event_url(calendar_id, event_id),
                method="DELETE",
                access_token=access_token,
            )
        except GoogleCalendarApiError as exc:
            if exc.status_code != 404:
                raise

    @staticmethod
    def _event_url(calendar_id: str, event_id: str) -> str:
        return (
            f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}"
            f"/events/{quote(event_id, safe='')}"
        )

    def _request_json(
        self,
        url: str,
        *,
        method: str,
        form: dict | None = None,
        json_body: dict | None = None,
        access_token: str | None = None,
    ) -> dict:
        data = None
        headers = {}
        if form is not None:
            data = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener(request, timeout=30) as response:
                raw = response.read()
                if not raw:
                    return {}
                return json.loads(raw.decode())
        except HTTPError as exc:
            try:
                error_body = exc.read().decode()[:500]
            except Exception:
                error_body = ""
            retryable = exc.code == 429 or 500 <= exc.code < 600
            raise GoogleCalendarApiError(
                (
                    f"Google Calendar request failed ({exc.code})"
                    f"{': ' + error_body if error_body else ''}"
                ),
                status_code=exc.code,
                retryable=retryable,
            ) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise GoogleCalendarApiError("Google Calendar request failed", retryable=True) from exc
