import uuid
import hmac
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.auth import authorize_user, get_current_user
from app.core.logging_config import configure_logging
from app.db import get_db
from app.agent.factory import create_agent
from app.channels.service import ChannelMessageService
from app.integrations.telegram.adapter import TelegramBotClient, TelegramUpdateError, parse_update
from app.reminders.serialization import to_response
from app.reminders.errors import DuplicateReminderError, ReminderInPastError
from app.reminders.service import ReminderService
from app.reminders.destinations import DestinationService
from app.reminders.canceling import ReminderCanceler
from app.reminders.rescheduling import ReminderRescheduler
from app.reminders.snoozing import ReminderSnoozer
from app.schemas import (
    AgentMessageRequest,
    AgentMessageResponse,
    ReminderCreate,
    ReminderCancelRequest,
    DeliveryDestinationCreate,
    DeliveryDestinationResponse,
    ReminderRescheduleRequest,
    ReminderQuery,
    ReminderResponse,
    SnoozeRequest,
    UserCreatedResponse,
    UserCreate,
)
from app.users.service import UserService
from app.models import User

app = FastAPI(title="Memoray Reminder and Memory Agent", version="0.1.0")
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
    occurrence_start: datetime | None = Query(default=None),
    occurrence_end: datetime | None = Query(default=None),
    status: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        authorize_user(user_id, current_user)
        query = ReminderQuery(
            title=title,
            start=start,
            end=end,
            occurrence_start=occurrence_start,
            occurrence_end=occurrence_end,
            status=status,
        )
        user = UserService(db).get(user_id)
        return [
            to_response(item, user.timezone, occurrence)
            for item, occurrence in ReminderService(db).list_with_occurrences(user_id, query)
        ]
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/users/{user_id}/destinations", response_model=DeliveryDestinationResponse, status_code=201)
def create_destination(
    user_id: uuid.UUID,
    data: DeliveryDestinationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        authorize_user(user_id, current_user)
        return DestinationService(db).create(user_id, data)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/users/{user_id}/reminders/{reminder_id}/reschedule", response_model=ReminderResponse)
def reschedule_reminder(
    user_id: uuid.UUID,
    reminder_id: uuid.UUID,
    data: ReminderRescheduleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        authorize_user(user_id, current_user)
        reminder = ReminderRescheduler(db).reschedule(user_id, reminder_id, data)
        user = UserService(db).get(user_id)
        return to_response(reminder, user.timezone)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReminderInPastError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/users/{user_id}/reminders/{reminder_id}/cancel", response_model=ReminderResponse)
def cancel_reminder(
    user_id: uuid.UUID,
    reminder_id: uuid.UUID,
    data: ReminderCancelRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        authorize_user(user_id, current_user)
        reminder = ReminderCanceler(db).cancel(user_id, reminder_id, data)
        user = UserService(db).get(user_id)
        return to_response(reminder, user.timezone)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/users/{user_id}/reminders/{reminder_id}/occurrences/{occurrence_id}/snooze",
    response_model=ReminderResponse,
)
def snooze_occurrence(
    user_id: uuid.UUID,
    reminder_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    data: SnoozeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        authorize_user(user_id, current_user)
        reminder = ReminderSnoozer(db).snooze(user_id, reminder_id, occurrence_id, data)
        user = UserService(db).get(user_id)
        return to_response(reminder, user.timezone)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/users/{user_id}/agent", response_model=AgentMessageResponse)
def agent_message(
    user_id: uuid.UUID,
    request: AgentMessageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        authorize_user(user_id, current_user)
        result = create_agent(db).handle(user_id, request.message, request.conversation_id)
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


@app.post("/webhooks/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_webhook_secret:
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, settings.telegram_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="invalid Telegram webhook secret")
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid Telegram JSON payload")
    try:
        incoming = parse_update(payload)
        channel_service = ChannelMessageService(db, create_agent)
        records = channel_service.process(incoming)
    except TelegramUpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    client = TelegramBotClient(settings.telegram_bot_token)
    for record in records:
        try:
            client.send_message(
                record.external_chat_id,
                record.content,
                request_phone=record.request_phone,
            )
        except RuntimeError as exc:
            channel_service.mark_failed(record.id, str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        channel_service.mark_sent(record.id)
    return {"ok": True}

