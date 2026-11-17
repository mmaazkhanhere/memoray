from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.mcp.auth.service import McpAuthService, OAuthError
from app.mcp.tools import TOOLS, execute, mcp_result
from app.models import McpOperationReceipt


router = APIRouter(tags=["mcp"])


@dataclass
class _Session:
    queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)


_sessions: dict[str, _Session] = {}


def _jsonrpc_response(request_id: Any, result: dict | None = None, error: dict | None = None) -> dict:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result or {}
    return payload


def _auth_context(authorization: str | None, db: Session):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise OAuthError("invalid_token", "Bearer access token required", status=401)
    identity = McpAuthService(db).verify_bearer(authorization[7:].strip())
    return identity


def _metadata_for_tool(name: str) -> dict | None:
    return next((item for item in TOOLS if item["name"] == name), None)


def _auth_headers() -> dict[str, str]:
    settings = get_settings()
    issuer = (settings.mcp_issuer or settings.mcp_public_url).rstrip("/")
    return {
        "WWW-Authenticate": f'Bearer resource_metadata="{issuer}/.well-known/oauth-protected-resource"'
    }


_MUTATIONS = {
    "create_reminder", "create_reminders", "reschedule_reminder", "cancel_reminder",
    "snooze_occurrence", "complete_delivered_occurrence", "create_memory",
}


def _arguments_hash(args: dict) -> str:
    canonical = {key: value for key, value in args.items() if key != "idempotency_key"}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()


def _process(payload: dict, db: Session, authorization: str | None) -> tuple[dict | None, int]:
    # A few MCP hosts omit the JSON-RPC version on their initial probe. Treat
    # this as a compatibility request while keeping all actual tool calls
    # authenticated below.
    if isinstance(payload, dict) and "jsonrpc" not in payload and "method" in payload:
        payload = {**payload, "jsonrpc": "2.0"}
    if payload.get("jsonrpc") != "2.0" or "method" not in payload:
        return _jsonrpc_response(payload.get("id"), error={"code": -32600, "message": "Invalid Request"}), 400
    method = payload["method"]
    request_id = payload.get("id")
    if request_id is None and method.startswith("notifications/"):
        return None, 202
    if method == "initialize":
        params = payload.get("params") or {}
        return _jsonrpc_response(request_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "memoray", "version": "0.1.0"},
            "instructions": (
                "Memoray is the authoritative reminder and memory store. Use list_reminders before "
                "mutations, pass exact IDs, and use the user's configured timezone."
            ),
        }), 200
    if method == "tools/list":
        return _jsonrpc_response(request_id, {"tools": TOOLS}), 200
    if method == "tools/call":
        params = payload.get("params") or {}
        name = params.get("name")
        tool = _metadata_for_tool(name)
        if tool is None:
            return _jsonrpc_response(request_id, error={"code": -32602, "message": "Unknown tool"}), 400
        try:
            identity = _auth_context(authorization, db)
            args = params.get("arguments") or {}
            operation_key = None
            if name in _MUTATIONS:
                supplied_key = args.get("idempotency_key") or str(request_id)
                if not supplied_key or supplied_key == "None":
                    raise ValueError("idempotency_key is required for mutation tools")
                operation_key = f"{name}:{supplied_key}"[:200]
                argument_hash = _arguments_hash(args)
                receipt = db.scalar(
                    select(McpOperationReceipt).where(
                        McpOperationReceipt.user_id == identity.user_id,
                        McpOperationReceipt.client_id == identity.client_id,
                        McpOperationReceipt.operation_key == operation_key,
                    )
                )
                if receipt is not None:
                    if receipt.arguments_hash != argument_hash:
                        raise ValueError("idempotency key was already used with different arguments")
                    return _jsonrpc_response(request_id, mcp_result(receipt.result)), 200
            result = execute(name, args, identity, db)
            if operation_key is not None:
                db.add(
                    McpOperationReceipt(
                        user_id=identity.user_id,
                        client_id=identity.client_id,
                        operation_key=operation_key,
                        arguments_hash=_arguments_hash(args),
                        result=result,
                    )
                )
            db.commit()
            return _jsonrpc_response(request_id, mcp_result(result)), 200
        except OAuthError as exc:
            db.rollback()
            if exc.status == 401:
                settings = get_settings()
                issuer = (settings.mcp_issuer or settings.mcp_public_url).rstrip("/")
                return _jsonrpc_response(
                    request_id,
                    error={
                        "code": -32001,
                        "message": exc.description,
                        "data": {
                            "_meta": {
                                "mcp/www_authenticate": (
                                    f'Bearer resource_metadata="{issuer}/.well-known/oauth-protected-resource", '
                                    'error="invalid_token", error_description="Bearer access token required"'
                                )
                            }
                        },
                    },
                ), 401
            return _jsonrpc_response(request_id, error={"code": -32003, "message": exc.description}), exc.status
        except Exception as exc:
            db.rollback()
            return _jsonrpc_response(request_id, error={"code": -32000, "message": str(exc)}), 500
    if method == "ping":
        return _jsonrpc_response(request_id, {}), 200
    return _jsonrpc_response(request_id, error={"code": -32601, "message": "Method not found"}), 404


@router.post("/mcp")
@router.post("/mcp/sse")
async def mcp_http(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if not get_settings().mcp_enabled:
        raise HTTPException(status_code=404, detail="MCP is not enabled")
    raw = await request.body()
    if not raw:
        # ChatGPT performs an empty POST probe before beginning OAuth/MCP
        # negotiation. It is not a JSON-RPC call and should not be rejected.
        return Response(status_code=200)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(_jsonrpc_response(None, error={"code": -32700, "message": "Parse error"}), 400)
    if isinstance(payload, list):
        responses = []
        statuses = []
        for item in payload:
            response, status = _process(item, db, authorization)
            if response is not None:
                responses.append(response)
            statuses.append(status)
        return JSONResponse(responses, status_code=max(statuses, default=202))
    response, status = _process(payload, db, authorization)
    if response is None:
        return JSONResponse({}, status_code=status)
    return JSONResponse(response, status_code=status, headers=_auth_headers() if status == 401 else None)


@router.get("/mcp/sse")
async def mcp_sse():
    if not get_settings().mcp_enabled:
        raise HTTPException(status_code=404, detail="MCP is not enabled")
    session_id = secrets.token_urlsafe(24)
    session = _Session()
    _sessions[session_id] = session

    async def events():
        yield f"event: endpoint\ndata: /mcp/messages?session_id={session_id}\n\n"
        try:
            while True:
                try:
                    message = await asyncio.wait_for(session.queue.get(), timeout=25)
                    yield f"event: message\ndata: {message}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _sessions.pop(session_id, None)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post("/mcp/messages")
async def mcp_messages(
    session_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if not get_settings().mcp_enabled:
        raise HTTPException(status_code=404, detail="MCP is not enabled")
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="MCP session not found")
    payload = await request.json()
    response, status = _process(payload, db, authorization)
    if response is not None:
        await session.queue.put(json.dumps(response, separators=(",", ":")))
    return JSONResponse(
        {}, status_code=202 if status < 400 else status,
        headers=_auth_headers() if status == 401 else None,
    )
