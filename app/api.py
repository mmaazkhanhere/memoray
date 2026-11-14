import uuid
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.orm import Session

from app.agent.service import ReminderAgent
from app.config import get_settings
from app.core.auth import authorize_user, get_current_user
from app.core.logging_config import configure_logging
from app.db import get_db
from app.llm.groq_provider import GroqProvider
from app.reminders.serialization import to_response
from app.reminders.errors import DuplicateReminderError, ReminderInPastError
from app.reminders.service import ReminderService
from app.schemas import (
    AgentMessageRequest,
    AgentMessageResponse,
    ReminderCreate,
    ReminderQuery,
    ReminderResponse,
    UserCreatedResponse,
    UserCreate,
)
from app.conversations.service import ConversationService
from app.users.service import UserService
from app.models import User

app = FastAPI(title="Memoray Reminder Agent", version="0.1.0")
configure_logging()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/users", response_model=UserCreatedResponse, status_code=201)
def create_user(data: UserCreate, db: Session = Depends(get_db)):
    try:
        user, access_token = UserService(db).create_with_token(data)
        return UserCreatedResponse(
            id=user.id,
            name=user.name,
            timezone=user.timezone,
            status=user.status,
            access_token=access_token,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/users/{user_id}/reminders", response_model=ReminderResponse, status_code=201)
def create_reminder(
    user_id: uuid.UUID,
    data: ReminderCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        authorize_user(user_id, current_user)
        reminder = ReminderService(db).create(user_id, data)
        user = UserService(db).get(user_id)
        return to_response(reminder, user.timezone)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DuplicateReminderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReminderInPastError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/users/{user_id}/reminders", response_model=list[ReminderResponse])
def list_reminders(
    user_id: uuid.UUID,
    title: str | None = Query(default=None),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    status: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        authorize_user(user_id, current_user)
        query = ReminderQuery(title=title, start=start, end=end, status=status)
        user = UserService(db).get(user_id)
        return [to_response(item, user.timezone) for item in ReminderService(db).list(user_id, query)]
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/users/{user_id}/agent", response_model=AgentMessageResponse)
def agent_message(
    user_id: uuid.UUID,
    request: AgentMessageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        authorize_user(user_id, current_user)
        provider = GroqProvider(get_settings())
        result = ReminderAgent(
            provider,
            ReminderService(db),
            UserService(db),
            ConversationService(db),
        ).handle(user_id, request.message, request.conversation_id)
        return {
            "conversation_id": result.conversation_id,
            "response": result.response,
            "draft_status": result.draft_status,
        }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

