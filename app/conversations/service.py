import uuid
from datetime import date, time

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ConversationMessageRole,
    ConversationStatus,
    DraftStatus,
    RecurrenceFrequency,
    ReminderConversation,
    ReminderConversationMessage,
    ReminderDraft,
)


class ConversationService:
    """Persists reminder-agent conversation context and its current draft."""

    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self, user_id: uuid.UUID, conversation_id: uuid.UUID | None) -> ReminderConversation:
        if conversation_id:
            conversation = self.db.scalar(
                select(ReminderConversation)
                .options(selectinload(ReminderConversation.draft), selectinload(ReminderConversation.messages))
                .where(ReminderConversation.id == conversation_id, ReminderConversation.user_id == user_id)
            )
            if conversation is None:
                raise LookupError("conversation not found")
            if conversation.status != ConversationStatus.active:
                conversation.status = ConversationStatus.active
            if conversation.draft is None:
                conversation.draft = ReminderDraft(status=DraftStatus.collecting)
            elif conversation.draft.status in {DraftStatus.created, DraftStatus.canceled}:
                conversation.draft.title = None
                conversation.draft.schedule_date = None
                conversation.draft.schedule_time = None
                conversation.draft.is_recurring = False
                conversation.draft.recurrence_frequency = None
                conversation.draft.recurrence_interval = 1
                conversation.draft.status = DraftStatus.collecting
            self.db.commit()
            return conversation

        conversation = ReminderConversation(user_id=user_id, status=ConversationStatus.active)
        conversation.draft = ReminderDraft(status=DraftStatus.collecting)
        self.db.add(conversation)
        self.db.commit()
        self.db.refresh(conversation)
        return conversation

    def record_message(self, conversation_id: uuid.UUID, role: ConversationMessageRole, content: str) -> None:
        self.db.add(
            ReminderConversationMessage(conversation_id=conversation_id, role=role, content=content)
        )
        self.db.commit()

    def recent_messages(self, conversation: ReminderConversation, limit: int = 12) -> list[dict[str, str]]:
        messages = conversation.messages[-limit:]
        return [{"role": message.role.value, "content": message.content} for message in messages]

    def update_draft(
        self,
        draft: ReminderDraft,
        *,
        title: str | None = None,
        schedule_date: date | None = None,
        schedule_time: time | None = None,
        is_recurring: bool | None = None,
        recurrence_frequency: RecurrenceFrequency | None = None,
        recurrence_interval: int | None = None,
    ) -> ReminderDraft:
        if draft.status in {DraftStatus.created, DraftStatus.canceled}:
            draft.title = None
            draft.schedule_date = None
            draft.schedule_time = None
            draft.is_recurring = False
            draft.recurrence_frequency = None
            draft.recurrence_interval = 1
        if title is not None:
            draft.title = title.strip()
        if schedule_date is not None:
            draft.schedule_date = schedule_date
        if schedule_time is not None:
            draft.schedule_time = schedule_time
        if is_recurring is not None:
            draft.is_recurring = is_recurring
        if recurrence_frequency is not None:
            if is_recurring is None:
                draft.is_recurring = True
            draft.recurrence_frequency = recurrence_frequency
        if recurrence_interval is not None:
            draft.recurrence_interval = recurrence_interval
        draft.status = DraftStatus.ready if self.is_complete(draft) else DraftStatus.collecting
        self.db.add(draft)
        self.db.commit()
        self.db.refresh(draft)
        return draft

    @staticmethod
    def is_complete(draft: ReminderDraft) -> bool:
        return bool(
            draft.title
            and draft.schedule_date
            and draft.schedule_time
            and (not draft.is_recurring or draft.recurrence_frequency)
        )

    @staticmethod
    def missing_fields(draft: ReminderDraft) -> list[str]:
        missing = []
        if not draft.title:
            missing.append("title")
        if not draft.schedule_date:
            missing.append("date")
        if not draft.schedule_time:
            missing.append("time")
        if draft.is_recurring and not draft.recurrence_frequency:
            missing.append("recurrence_frequency")
        return missing

    def mark_created(self, draft: ReminderDraft, conversation: ReminderConversation) -> None:
        draft.status = DraftStatus.created
        conversation.status = ConversationStatus.completed
        self.db.commit()

