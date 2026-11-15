import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import DeliveryChannel, OccurrenceStatus, RecurrenceFrequency, ReminderStatus, UserStatus


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    timezone: str = Field(min_length=1, max_length=64)
    status: UserStatus = UserStatus.active

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class UserResponse(UserCreate):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class UserCreatedResponse(UserResponse):
    access_token: str


class AgentMessageRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: uuid.UUID | None = None


class AgentMessageResponse(BaseModel):
    conversation_id: uuid.UUID
    response: str
    draft_status: str | None = None


class ReminderCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    schedule: datetime
    is_recurring: bool = False
    recurrence_rule: str | None = None
    recurrence_frequency: RecurrenceFrequency | None = None
    recurrence_interval: int = Field(default=1, ge=1, le=365)
    destination_id: uuid.UUID | None = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value


class ReminderListStatus(str, Enum):
    pending = "pending"
    completed = "completed"


class ReminderQuery(BaseModel):
    title: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    occurrence_start: datetime | None = None
    occurrence_end: datetime | None = None
    status: ReminderListStatus | None = None


class DeliveryDestinationCreate(BaseModel):
    channel: DeliveryChannel
    identity: str = Field(min_length=1, max_length=500)
    is_linked: bool = True
    is_valid: bool = True


class DeliveryDestinationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    user_id: uuid.UUID
    channel: DeliveryChannel
    identity: str
    is_linked: bool
    is_valid: bool


class RescheduleScope(str, Enum):
    series = "series"
    occurrence = "occurrence"


class ReminderRescheduleRequest(BaseModel):
    schedule: datetime
    scope: RescheduleScope | None = None
    occurrence_id: uuid.UUID | None = None
    destination_id: uuid.UUID | None = None


class SnoozeRequest(BaseModel):
    duration_seconds: int = Field(gt=0)


class OccurrenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    schedule: datetime
    next_due_at: datetime
    status: OccurrenceStatus


class ReminderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    schedule: datetime
    is_recurring: bool
    recurrence_rule: str | None
    recurrence_frequency: RecurrenceFrequency | None
    recurrence_interval: int
    destination_id: uuid.UUID | None = None
    status: ReminderStatus
    occurrence: OccurrenceResponse | None = None

