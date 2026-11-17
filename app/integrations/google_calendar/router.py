from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.auth import authorize_user, get_current_user
from app.db import get_db
from app.integrations.google_calendar.errors import (
    CalendarNotConfigured,
    GoogleCalendarApiError,
    OAuthStateError,
)
from app.integrations.google_calendar.service import GoogleCalendarService
from app.models import User


router = APIRouter(tags=["google-calendar"])


@router.post("/users/{user_id}/integrations/google-calendar/connect")
def begin_google_calendar_connection(
    user_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    authorize_user(user_id, current_user)
    try:
        url = GoogleCalendarService(db).begin_connection(user_id)
        db.commit()
        return {"authorization_url": url}
    except CalendarNotConfigured as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/integrations/google-calendar/oauth/callback", response_class=HTMLResponse)
@router.get("/auth/google/callback", response_class=HTMLResponse)
def google_calendar_callback(
    state: str = Query(default=""),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    try:
        GoogleCalendarService(db).finish_connection(state=state, code=code, error=error)
        return HTMLResponse(
            "<h1>Google Calendar connected</h1>"
            "<p>You can close this window and continue using Memoray.</p>"
        )
    except OAuthStateError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CalendarNotConfigured as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GoogleCalendarApiError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail="Google Calendar authorization failed") from exc
