from __future__ import annotations

import html
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.mcp.auth.service import ALL_SCOPES, McpAuthService, OAuthError, _valid_redirect_uri, _resource


router = APIRouter(tags=["mcp-auth"])


def _require_enabled() -> None:
    if not get_settings().mcp_enabled:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="MCP is not enabled")


def _error_response(exc: OAuthError):
    return JSONResponse(
        {"error": exc.error, "error_description": exc.description}, status_code=exc.status
    )


@router.get("/.well-known/oauth-protected-resource")
def protected_resource_metadata():
    _require_enabled()
    settings = get_settings()
    issuer = (settings.mcp_issuer or settings.mcp_public_url).rstrip("/")
    return {
        "resource": f"{issuer}/mcp",
        "authorization_servers": [issuer],
        "scopes_supported": sorted(ALL_SCOPES),
        "bearer_methods_supported": ["header"],
    }


@router.get("/mcp/sse/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp/sse")
def protected_resource_metadata_for_mcp_path():
    return protected_resource_metadata()


@router.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata():
    _require_enabled()
    settings = get_settings()
    issuer = (settings.mcp_issuer or settings.mcp_public_url).rstrip("/")
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "registration_endpoint": f"{issuer}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [
            "memoray:reminders.read",
            "memoray:reminders.write",
            "memoray:memories.read",
            "memoray:memories.write",
        ],
    }


@router.get("/.well-known/openid-configuration")
def openid_configuration():
    return authorization_server_metadata()


@router.get("/mcp/sse/.well-known/openid-configuration")
@router.get("/.well-known/openid-configuration/mcp/sse")
def openid_configuration_for_mcp_path():
    return authorization_server_metadata()


@router.get("/mcp/sse/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp/sse")
def authorization_server_metadata_for_mcp_path():
    return authorization_server_metadata()


@router.post("/oauth/register")
async def register_client(request: Request):
    _require_enabled()
    try:
        body = await request.json()
        redirect_uris = body.get("redirect_uris") or []
        if not redirect_uris or not all(
            isinstance(uri, str) and _valid_redirect_uri(uri)
            for uri in redirect_uris
        ):
            raise ValueError
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    client_id = f"urn:memoray:client:{__import__('secrets').token_urlsafe(18)}"
    return {
        "client_id": client_id,
        "client_id_issued_at": 0,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
    }


@router.get("/oauth/authorize", response_class=HTMLResponse)
def authorize(
    client_id: str,
    redirect_uri: str,
    response_type: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    scope: str,
    resource: str = "",
    db: Session = Depends(get_db),
):
    _require_enabled()
    try:
        if response_type != "code":
            raise OAuthError("unsupported_response_type", "only authorization code is supported")
        _attempt, nonce = McpAuthService(db).begin_authorization(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=scope,
            resource=resource,
        )
        db.commit()
        settings = get_settings()
        bot_username = getattr(settings, "telegram_bot_username", None) or "your_memoray_bot"
        deep_link = f"https://t.me/{bot_username}?start=mcp_{nonce}"
        safe_nonce = html.escape(nonce, quote=True)
        safe_link = html.escape(deep_link, quote=True)
        return HTMLResponse(
            "<!doctype html><meta name='viewport' content='width=device-width'>"
            "<title>Connect Memoray</title><main style='font-family:sans-serif;max-width:42rem;margin:4rem auto'>"
            "<h1>Connect Memoray to ChatGPT</h1>"
            "<p>Open the Memoray Telegram bot, then approve this connection:</p>"
            f"<p><a href='{safe_link}'>Open Telegram</a></p>"
            f"<p>Or send <code>/connect_chatgpt {safe_nonce}</code> to your bot.</p>"
            "<p id='status'>Waiting for Telegram approval…</p>"
            "<script>"
            f"const nonce='{safe_nonce}';"
            "async function poll(){const r=await fetch('/oauth/authorize/status?nonce='+encodeURIComponent(nonce));"
            "const d=await r.json(); if(d.redirect){location.href=d.redirect;return;}"
            "if(d.error){document.getElementById('status').textContent=d.error;return;}"
            "setTimeout(poll,1200)} poll();"
            "</script></main>"
        )
    except OAuthError as exc:
        db.rollback()
        return _error_response(exc)


@router.get("/oauth/authorize/status")
def authorization_status(nonce: str, db: Session = Depends(get_db)):
    _require_enabled()
    try:
        redirect = McpAuthService(db).poll_redirect(nonce)
        db.commit()
        return {"redirect": redirect, "pending": redirect is None}
    except OAuthError as exc:
        db.rollback()
        return _error_response(exc)


@router.post("/oauth/token")
async def token(request: Request, db: Session = Depends(get_db)):
    _require_enabled()
    try:
        raw = await request.body()
        values = {key: items[-1] for key, items in parse_qs(raw.decode()).items()}
        service = McpAuthService(db)
        grant = values.get("grant_type")
        if grant == "authorization_code":
            result = service.exchange_code(
                code=values.get("code", ""),
                client_id=values.get("client_id", ""),
                redirect_uri=values.get("redirect_uri", ""),
                code_verifier=values.get("code_verifier", ""),
                resource=_resource(values.get("resource", "") or service.issuer + "/mcp"),
            )
        elif grant == "refresh_token":
            result = service.refresh(
                refresh_token=values.get("refresh_token", ""),
                client_id=values.get("client_id", ""),
                resource=_resource(values.get("resource", "") or service.issuer + "/mcp"),
            )
        else:
            raise OAuthError("unsupported_grant_type", "unsupported grant type")
        db.commit()
        return result
    except OAuthError as exc:
        db.rollback()
        return _error_response(exc)
