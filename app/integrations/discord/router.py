from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations.discord.errors import DiscordNotConfigured, DiscordOAuthError
from app.integrations.discord.linking import DiscordLinkService


router = APIRouter(tags=["discord"])


@router.get("/integrations/discord/oauth/callback", response_class=HTMLResponse)
def discord_oauth_callback(
    state: str = Query(default=""),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    try:
        DiscordLinkService(db).complete(state=state, code=code, error=error)
        return HTMLResponse(
            "<h1>Discord connected</h1>"
            "<p>Your Discord account is connected. You can close this window and message the bot.</p>"
        )
    except DiscordOAuthError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DiscordNotConfigured as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
