from __future__ import annotations

import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.routing import APIRoute
from starlette.responses import Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.integrations.vapi.assistant import assistant_configuration
from app.integrations.vapi.service import VapiAuthenticationError, VapiCallService
from app.urgent_reminders.service import UrgentReminderService
from app.time_utils import to_user_timezone

logger = logging.getLogger(__name__)


class VapiDiagnosticRoute(APIRoute):
    """Temporarily expose request arrival and outcome without logging payloads."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def diagnostic_handler(request: Request) -> Response:
            started = time.monotonic()
            arrival_time = datetime.now(timezone.utc).isoformat()
            message_type: str | None = None
            status_code = 500
            _log_event(
                logging.INFO,
                "request_started",
                arrival_time=arrival_time,
                method=request.method,
                path=request.url.path,
            )
            try:
                # Request.body() is cached by Starlette, so the endpoint's
                # existing request.json() call receives the same body.
                raw_body = await request.body()
                try:
                    parsed = json.loads(raw_body)
                    if isinstance(parsed, dict):
                        message = parsed.get("message")
                        if isinstance(message, dict) and isinstance(message.get("type"), str):
                            message_type = message["type"]
                        elif isinstance(parsed.get("type"), str):
                            message_type = parsed["type"]
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
                response = await handler(request)
                status_code = response.status_code
                return response
            except HTTPException as exc:
                status_code = exc.status_code
                raise
            finally:
                _log_event(
                    logging.INFO,
                    "request_completed",
                    arrival_time=arrival_time,
                    message_type=message_type,
                    status_code=status_code,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )

        return diagnostic_handler


router = APIRouter(route_class=VapiDiagnosticRoute)


def _call_id(call: dict[str, Any] | None) -> str:
    """Return an opaque identifier suitable for correlating application logs."""
    if not call:
        return "missing"
    return str(call.get("id") or "missing")


def _log_event(level: int, event: str, **context: Any) -> None:
    """Log Vapi lifecycle events without recording secrets or call content."""
    logger.log(level, "vapi.%s %s", event, context)


_DIAGNOSTIC_REDACTED_FIELDS = {"content", "query", "candidates", "transcript"}


def _diagnostic_data(value: Any, *, field_name: str | None = None) -> Any:
    """Keep phone diagnostics useful without writing memories or transcripts to logs."""
    if field_name in _DIAGNOSTIC_REDACTED_FIELDS:
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(key): _diagnostic_data(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_diagnostic_data(item) for item in value]
    if isinstance(value, str) and len(value) > 1_000:
        return f"{value[:1_000]}… <truncated>"
    return value


def _call_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    call = message.get("call") if isinstance(message.get("call"), dict) else payload.get("call")
    if not isinstance(call, dict):
        raise ValueError("Vapi call is required")
    return message, call


def _caller_number(call: dict[str, Any]) -> str | None:
    from_data = call.get("from")
    if isinstance(from_data, dict) and from_data.get("phoneNumber"):
        return str(from_data["phoneNumber"])
    customer = call.get("customer")
    if isinstance(customer, dict) and customer.get("number"):
        return str(customer["number"])
    return None


def _assistant_error(message: str) -> dict[str, Any]:
    return _message_response({"error": message})


def _message_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a Vapi Server Message response in the required response envelope."""
    return {"messageResponse": payload}


def _tool_results_response(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Return Vapi Function-tool results in their required top-level envelope.

    ``messageResponse`` is used by server-message responses such as an
    assistant-request error.  Function tool calls are different: Vapi reads
    ``results`` directly from the HTTP response body and matches each item by
    ``toolCallId``.  Nesting it in ``messageResponse`` makes Vapi treat every
    tool call as having no result.
    """
    return {"results": results}


def _tool_result(
    *,
    name: str,
    tool_call_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Convert an application result into Vapi's per-tool success/error shape."""
    if result.get("error") or result.get("operation") in {"failed", "rejected"}:
        return {
            "name": name,
            "toolCallId": tool_call_id,
            "error": str(result.get("error") or "Tool execution failed."),
        }
    return {
        "name": name,
        "toolCallId": tool_call_id,
        "result": json.dumps(result, separators=(",", ":")),
    }


def _tool_call_errors(message: dict[str, Any], error: str) -> list[dict[str, Any]]:
    """Create one Vapi error result for every tool call with an ID."""
    results = []
    for item in _tool_calls(message):
        tool_call_id = str(item.get("id") or "")
        if not tool_call_id:
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = str(function.get("name") or "")
        result = {"toolCallId": tool_call_id, "error": error}
        if name:
            result["name"] = name
        results.append(result)
    return results


def _assistant_response(call, settings):
    assistant_id = settings.vapi_assistant_id
    if not assistant_id:
        raise ValueError("VAPI_ASSISTANT_ID is not configured")
    timezone_name = call.user.timezone or "UTC"
    current_local_time = to_user_timezone(datetime.now(timezone.utc), timezone_name)
    # Render time-sensitive context on our server.  Vapi variable substitution
    # is retained below for other assistant fields, but a rendered system
    # message prevents the model from falling back to stale training-time dates
    # if a provider does not expand a prompt variable for an inbound call.
    model = assistant_configuration(
        user_name=call.user.name,
        user_timezone=timezone_name,
        current_local_time=current_local_time.isoformat(),
    )["model"]
    response = {
        "assistantId": assistant_id,
        "assistantOverrides": {
            "model": model,
            "variableValues": {
                "user_name": call.user.name or "there",
                "user_timezone": timezone_name,
                "current_local_time": current_local_time.isoformat(),
            }
        },
    }
    _log_event(
        logging.INFO,
        "assistant_response_sent",
        response_keys=sorted(response),
        assistant_id=assistant_id,
        user_timezone=timezone_name,
        current_local_time=current_local_time.isoformat(),
        model_override_includes_time=current_local_time.isoformat()
        in model["messages"][0]["content"],
        variable_values=response["assistantOverrides"]["variableValues"],
    )
    return response


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("toolCallList") or []
    if not calls and isinstance(message.get("toolWithToolCallList"), list):
        calls = [item.get("toolCall") for item in message["toolWithToolCallList"]]
    if not isinstance(calls, list):
        raise ValueError("Vapi tool calls are invalid")
    return [item for item in calls if isinstance(item, dict)]


def _parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Vapi tool arguments are invalid")


def _urgent_correlation_key(message: dict[str, Any], call: dict[str, Any]) -> str | None:
    for source in (call.get("metadata"), message.get("metadata")):
        if isinstance(source, dict):
            value = source.get("urgent_attempt_key") or source.get("urgentAttemptKey")
            if value:
                return str(value)
    return None


def _urgent_outcome(message: dict[str, Any], call: dict[str, Any]) -> tuple[str, str | None] | None:
    values = [
        call.get("answeredBy"),
        call.get("answered_by"),
        message.get("answeredBy"),
        message.get("answered_by"),
    ]
    answered_by = next((str(value).casefold() for value in values if value), "")
    if answered_by:
        if any(term in answered_by for term in ("human", "person")):
            return "answered", answered_by
        if any(term in answered_by for term in ("machine", "voicemail", "fax", "unknown")):
            return "not_picked", answered_by

    reason = str(message.get("endedReason") or call.get("endedReason") or "").casefold()
    status = str(message.get("status") or call.get("status") or "").casefold()
    if not reason and status not in {"ended", "completed"}:
        return None
    if any(term in reason for term in ("did-not-answer", "no-answer", "busy", "voicemail", "machine")):
        return "not_picked", reason or "not_picked"
    if reason in {
        "assistant-ended-call", "assistant-said-end-call", "customer-ended-call",
    } or reason.startswith("assistant"):
        return "answered", reason or "completed"
    if not reason and status in {"ended", "completed"}:
        return "failed", "missing_ended_reason"
    return "failed", reason


def _outbound_assistant_response(attempt, settings) -> dict[str, Any]:
    assistant_id = settings.vapi_outbound_assistant_id
    if not assistant_id:
        raise ValueError("VAPI_OUTBOUND_ASSISTANT_ID is not configured")
    reminder = attempt.delivery.reminder
    user = reminder.user
    return {
        "assistantId": assistant_id,
        "assistantOverrides": {
            "variableValues": {
                "user_name": user.name or "there",
                "reminder_text": f"Urgent reminder: {reminder.title}. Please complete this task.",
            }
        },
    }


@router.post("/webhooks/vapi")
async def vapi_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    started = time.monotonic()
    settings = get_settings()
    expected = settings.vapi_webhook_secret
    if not expected or not authorization:
        _log_event(logging.WARNING, "credential_rejected", reason="missing")
        raise HTTPException(status_code=401, detail="invalid Vapi webhook credential")
    scheme, _, supplied = authorization.partition(" ")
    if scheme.casefold() != "bearer":
        _log_event(logging.WARNING, "credential_rejected", reason="scheme")
        raise HTTPException(status_code=401, detail="invalid Vapi webhook credential")
    supplied = supplied.strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        _log_event(logging.WARNING, "credential_rejected", reason="mismatch")
        raise HTTPException(status_code=401, detail="invalid Vapi webhook credential")
    try:
        payload = await request.json()
    except ValueError as exc:
        _log_event(logging.WARNING, "payload_rejected", reason="invalid_json")
        raise HTTPException(status_code=400, detail="invalid Vapi JSON payload") from exc
    if not isinstance(payload, dict):
        _log_event(logging.WARNING, "payload_rejected", reason="not_object")
        raise HTTPException(status_code=400, detail="invalid Vapi payload")

    message: dict[str, Any] = payload
    try:
        message, call_data = _call_payload(payload)
        message_type = message.get("type")
        call_id = str(call_data.get("id") or "")
        log_call_id = _call_id(call_data)
        _log_event(logging.INFO, "received", type=message_type, call_id=log_call_id)
        urgent_service = UrgentReminderService(
            db, retry_delay_seconds=settings.urgent_call_retry_delay_seconds
        )
        urgent_attempt = urgent_service.find_attempt(
            external_call_id=call_id or None,
            correlation_key=_urgent_correlation_key(message, call_data),
        )
        if urgent_attempt is not None:
            if message_type == "assistant-request":
                return _outbound_assistant_response(urgent_attempt, settings)
            if message_type in {"status-update", "end-of-call-report"}:
                outcome = _urgent_outcome(message, call_data)
                if outcome is not None:
                    urgent_service.handle_provider_outcome(
                        external_call_id=call_id or None,
                        correlation_key=urgent_attempt.correlation_key,
                        outcome=outcome[0],
                        detail=outcome[1],
                    )
            return {}
        service = VapiCallService(db)
        if message_type == "assistant-request":
            number = _caller_number(call_data)
            if not number:
                _log_event(logging.WARNING, "assistant_rejected", call_id=log_call_id, reason="missing_caller")
                return _assistant_error(
                    "Please call from the phone number linked to your Memoray account."
                )
            try:
                session = service.authenticate_caller(call_id, number)
            except VapiAuthenticationError as exc:
                _log_event(logging.WARNING, "assistant_rejected", call_id=log_call_id, reason=str(exc))
                return _assistant_error(str(exc))
            if time.monotonic() - started > settings.vapi_assistant_request_deadline_seconds:
                _log_event(logging.ERROR, "assistant_rejected", call_id=log_call_id, reason="deadline")
                return _assistant_error("Memoray could not verify this call in time.")
            _log_event(
                logging.INFO,
                "assistant_selected",
                call_id=log_call_id,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return _assistant_response(session, settings)

        if message_type == "tool-calls":
            if not call_id:
                _log_event(logging.WARNING, "tool_calls_rejected", reason="missing_call_id")
                return _tool_results_response(
                    _tool_call_errors(message, "Vapi call ID is required.")
                )
            try:
                session = service.authenticated_call(call_id, _caller_number(call_data))
            except VapiAuthenticationError as exc:
                _log_event(logging.WARNING, "tool_calls_rejected", call_id=log_call_id, reason=str(exc))
                return _tool_results_response(_tool_call_errors(message, str(exc)))
            results = []
            tool_calls = _tool_calls(message)
            _log_event(
                logging.INFO,
                "tool_calls_received",
                call_id=log_call_id,
                count=len(tool_calls),
                tools=[
                    {
                        "tool": (
                            item.get("function", {}).get("name")
                            if isinstance(item.get("function"), dict)
                            else None
                        ),
                        "tool_call_id": item.get("id"),
                    }
                    for item in tool_calls
                ],
            )
            for item in tool_calls:
                function = item.get("function") if isinstance(item.get("function"), dict) else {}
                tool_call_id = str(item.get("id") or "")
                name = str(function.get("name") or "")
                if not tool_call_id or not name:
                    continue
                database = db.get_bind().url.render_as_string(hide_password=True)
                _log_event(
                    logging.INFO,
                    "tool_execution_started",
                    call_id=log_call_id,
                    user_id=str(session.user_id),
                    tool=name,
                    tool_call_id=tool_call_id,
                    raw_arguments=_diagnostic_data(function.get("arguments", {})),
                    database=database,
                )
                try:
                    arguments = _parse_arguments(function.get("arguments", {}))
                    _log_event(
                        logging.INFO,
                        "tool_arguments_parsed",
                        call_id=log_call_id,
                        tool=name,
                        tool_call_id=tool_call_id,
                        arguments=_diagnostic_data(arguments),
                    )
                    result = service.execute_tool(
                        session,
                        tool_call_id=tool_call_id,
                        name=name,
                        arguments=arguments,
                    )
                except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    _log_event(
                        logging.WARNING,
                        "tool_execution_rejected",
                        call_id=log_call_id,
                        tool=name,
                        tool_call_id=tool_call_id,
                        error=str(exc),
                    )
                    results.append(
                        {
                            "name": name,
                            "toolCallId": tool_call_id,
                            "error": str(exc),
                        }
                    )
                    continue
                _log_event(
                    logging.INFO,
                    "tool_completed",
                    call_id=log_call_id,
                    tool=name,
                    tool_call_id=tool_call_id,
                    operation=result.get("operation"),
                    result=_diagnostic_data(result),
                )
                vapi_result = _tool_result(name=name, tool_call_id=tool_call_id, result=result)
                _log_event(
                    logging.INFO,
                    "tool_result_sent",
                    call_id=log_call_id,
                    tool=name,
                    tool_call_id=tool_call_id,
                    response=_diagnostic_data(vapi_result),
                )
                results.append(vapi_result)
            return _tool_results_response(results)

        if message_type == "status-update":
            _log_event(
                logging.INFO,
                "status_updated",
                call_id=log_call_id,
                status=message.get("status") or call_data.get("status"),
                ended_reason=message.get("endedReason") or call_data.get("endedReason"),
                error=message.get("error") if isinstance(message.get("error"), str) else None,
            )
        elif message_type == "assistant.started":
            _log_event(logging.INFO, "assistant_started", call_id=log_call_id)
        elif message_type == "end-of-call-report":
            service.end_call(call_id)
            _log_event(
                logging.INFO,
                "ended",
                call_id=log_call_id,
                ended_reason=message.get("endedReason") or call_data.get("endedReason"),
                has_artifact=isinstance(message.get("artifact"), dict),
                has_transcript=bool(message.get("artifact", {}).get("transcript"))
                if isinstance(message.get("artifact"), dict)
                else False,
            )
        else:
            _log_event(logging.INFO, "ignored", type=message_type, call_id=log_call_id)
        return {}
    except Exception:
        db.rollback()
        logger.exception(
            "vapi.handler_failed type=%s call_id=%s elapsed_ms=%s",
            message.get("type"),
            _call_id(message.get("call") if isinstance(message.get("call"), dict) else None),
            round((time.monotonic() - started) * 1000),
        )
        if message.get("type") == "assistant-request":
            return _assistant_error("Memoray could not verify this call.")
        if message.get("type") == "tool-calls":
            return _tool_results_response(
                _tool_call_errors(message, "The tool call could not be completed.")
            )
        return {}
