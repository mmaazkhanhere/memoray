import uuid
import hashlib
import json
import inspect
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.service import ReminderAgent
from app.channels.contracts import IncomingMessage, OutgoingMessage
from app.models import (
    ChannelAccount,
    ChannelAccountState,
    ChannelIdentity,
    ChannelIdentityStatus,
    ChannelKind,
    InboundEvent,
    InboundEventStatus,
    OutboundMessage,
)
from app.onboarding.service import OnboardingService
from app.reminders.completion import CompletionService


class ChannelMessageService:
    """Coordinates channel identity, onboarding, idempotency, and agent routing."""

    def __init__(self, db: Session, agent_factory: Callable[[Session], ReminderAgent]):
        self.db = db
        self.agent_factory = agent_factory

    def process(
        self, message: IncomingMessage, *, create_account: bool = True
    ) -> list[OutboundMessage]:
        event = self.claim(message)
        if event is None:
            return self.pending_for(message.channel, message.external_chat_id)
        return self.process_claimed(message, event, create_account=create_account)

    def claim(self, message: IncomingMessage) -> InboundEvent | None:
        """Durably claim an update before any external work is performed."""
        try:
            event = self._claim_event(message)
            if event is None:
                return None
            self.db.commit()
            return event
        except Exception:
            self.db.rollback()
            raise

    def process_claimed(
        self,
        message: IncomingMessage,
        event: InboundEvent,
        *,
        create_account: bool = True,
    ) -> list[OutboundMessage]:
        try:
            account = self._account_for(message, create_account=create_account)
            if account is None:
                replies = [
                    OutgoingMessage(
                        message.channel,
                        message.external_chat_id,
                        "This Discord account is not linked yet. In Telegram, send /connect_discord and then authorize Discord.",
                    )
                ]
            else:
                onboarding = OnboardingService(self.db)
                if account.onboarding_state != ChannelAccountState.completed:
                    result = onboarding.handle(account, message)
                    replies = result.replies if result.handled else []
                else:
                    phone_result = onboarding.handle_completed(account, message)
                    if phone_result.handled:
                        replies = phone_result.replies
                    elif message.completion_occurrence_id is not None:
                        completion = CompletionService(self.db).complete_by_id(
                            account.user_id,
                            message.completion_occurrence_id,
                        )
                        replies = [
                            OutgoingMessage(
                                message.channel,
                                message.external_chat_id,
                                self._completion_reply(completion),
                            )
                        ]
                    elif message.text and message.text.strip().casefold() == "/start":
                        replies = [
                            OutgoingMessage(
                                message.channel, message.external_chat_id, "Welcome back! How can I help?"
                            )
                        ]
                    else:
                        identity = self.db.scalar(
                            select(ChannelIdentity).where(
                                ChannelIdentity.user_id == account.user_id,
                                ChannelIdentity.channel == account.channel,
                                ChannelIdentity.external_principal_id == account.external_user_id,
                                ChannelIdentity.status == ChannelIdentityStatus.linked,
                            )
                        )
                        agent = self._agent_for(identity)
                        handle = agent.handle
                        if "commit" in inspect.signature(handle).parameters:
                            result = handle(
                                account.user_id,
                                message.text or "",
                                account.conversation_id,
                                commit=False,
                            )
                        else:
                            result = handle(
                                account.user_id,
                                message.text or "",
                                account.conversation_id,
                            )
                        account.conversation_id = result.conversation_id
                        replies = [
                            OutgoingMessage(message.channel, message.external_chat_id, result.response)
                        ]

            records = [
                OutboundMessage(
                    inbound_event_id=event.id,
                    channel=ChannelKind(reply.channel),
                    external_chat_id=reply.external_chat_id,
                    content=reply.text,
                    request_phone=reply.request_phone,
                )
                for reply in replies
            ]
            self.db.add_all(records)
            event.status = InboundEventStatus.completed
            event.completed_at = datetime.now(timezone.utc)
            self.db.commit()
            # Include older unsent replies so a later webhook retries the
            # durable outbox without rerunning the business operation.
            return self.pending_for(message.channel, message.external_chat_id)
        except Exception:
            self.db.rollback()
            raise

    def _agent_for(self, identity: ChannelIdentity | None) -> ReminderAgent:
        """Preserve compatibility with small test/demonstration factories."""
        try:
            return self.agent_factory(self.db, notification_identity=identity)
        except TypeError as exc:
            if "notification_identity" not in str(exc):
                raise
            return self.agent_factory(self.db)

    @staticmethod
    def _completion_reply(result) -> str:
        if result.operation == "completed":
            return f"Done — I marked {result.reminder.title} as completed."
        if result.operation == "already_completed":
            return f"{result.reminder.title} was already completed."
        if result.operation == "not_delivered":
            return "I can only mark a reminder done after it has been delivered."
        if result.operation == "not_found":
            return "I couldn't find that reminder occurrence."
        return "I couldn't mark that reminder done."

    def complete_claimed(
        self,
        event: InboundEvent,
        reply: OutgoingMessage,
    ) -> list[OutboundMessage]:
        """Persist a non-agent reply for a claimed event, such as STT failure."""
        try:
            self.db.add(
                OutboundMessage(
                    inbound_event_id=event.id,
                    channel=ChannelKind(reply.channel),
                    external_chat_id=reply.external_chat_id,
                    content=reply.text,
                    request_phone=reply.request_phone,
                )
            )
            event.status = InboundEventStatus.completed
            event.completed_at = datetime.now(timezone.utc)
            self.db.commit()
            return self.pending_for(reply.channel, reply.external_chat_id)
        except Exception:
            self.db.rollback()
            raise

    def _claim_event(self, message: IncomingMessage) -> InboundEvent | None:
        payload_hash = hashlib.sha256(
            json.dumps(
                {
                    "channel": message.channel,
                    "update_id": message.external_update_id,
                    "user_id": message.external_user_id,
                    "chat_id": message.external_chat_id,
                    "text": message.text,
                    "completion_occurrence_id": (
                        str(message.completion_occurrence_id)
                        if message.completion_occurrence_id is not None
                        else None
                    ),
                    "callback_query_id": message.callback_query_id,
                    "callback_message_id": message.callback_message_id,
                    "voice": (
                        {
                            "file_id": message.voice.file_id,
                            "file_unique_id": message.voice.file_unique_id,
                            "mime_type": message.voice.mime_type,
                            "duration_seconds": message.voice.duration_seconds,
                            "file_size": message.voice.file_size,
                        }
                        if message.voice is not None
                        else None
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        existing = self.db.scalar(
            select(InboundEvent).where(
                InboundEvent.channel == ChannelKind(message.channel),
                InboundEvent.external_update_id == message.external_update_id,
            )
        )
        if existing is not None:
            if existing.payload_hash and existing.payload_hash != payload_hash:
                raise ValueError("inbound event ID was reused with a different payload")
            return None
        event = InboundEvent(
            channel=ChannelKind(message.channel),
            external_update_id=message.external_update_id,
            payload_hash=payload_hash,
            status=InboundEventStatus.processing,
        )
        try:
            with self.db.begin_nested():
                self.db.add(event)
                self.db.flush()
        except IntegrityError:
            self.db.expire_all()
            return None
        return event

    def _account_for(
        self, message: IncomingMessage, *, create_account: bool = True
    ) -> ChannelAccount | None:
        account = self.db.scalar(
            select(ChannelAccount).where(
                ChannelAccount.channel == ChannelKind(message.channel),
                ChannelAccount.external_user_id == message.external_user_id,
            )
        )
        if account is not None:
            account.external_chat_id = message.external_chat_id
            self._sync_identity(account)
            return account
        if not create_account:
            return None
        onboarding = OnboardingService(self.db)
        try:
            with self.db.begin_nested():
                account = onboarding.create_account(message)
        except IntegrityError:
            account = self.db.scalar(
                select(ChannelAccount).where(
                    ChannelAccount.channel == ChannelKind(message.channel),
                    ChannelAccount.external_user_id == message.external_user_id,
                )
            )
            if account is None:
                raise
        self._sync_identity(account)
        return account

    def _sync_identity(self, account: ChannelAccount) -> ChannelIdentity:
        identity = self.db.scalar(
            select(ChannelIdentity).where(
                ChannelIdentity.channel == account.channel,
                ChannelIdentity.external_principal_id == account.external_user_id,
            )
        )
        if identity is None:
            identity = ChannelIdentity(
                user_id=account.user_id,
                channel=account.channel,
                external_principal_id=account.external_user_id,
                external_destination_id=account.external_chat_id,
                status=ChannelIdentityStatus.linked,
                is_default=True,
                verified_at=datetime.now(timezone.utc),
            )
            self.db.add(identity)
        else:
            identity.external_destination_id = account.external_chat_id
            identity.status = ChannelIdentityStatus.linked
            identity.verified_at = identity.verified_at or datetime.now(timezone.utc)
        self.db.flush()
        return identity

    def pending_for(self, channel: str, external_chat_id: str) -> list[OutboundMessage]:
        return list(
            self.db.scalars(
                select(OutboundMessage).where(
                    OutboundMessage.channel == ChannelKind(channel),
                    OutboundMessage.external_chat_id == external_chat_id,
                    OutboundMessage.sent_at.is_(None),
                ).order_by(OutboundMessage.created_at.asc())
            )
        )

    def mark_sent(self, message_id: uuid.UUID) -> None:
        message = self.db.get(OutboundMessage, message_id)
        if message is None:
            return
        message.sent_at = datetime.now(timezone.utc)
        message.attempts += 1
        message.last_error = None
        self.db.commit()

    def mark_failed(self, message_id: uuid.UUID, error: str) -> None:
        message = self.db.get(OutboundMessage, message_id)
        if message is None:
            return
        message.attempts += 1
        message.last_error = error[:1000]
        self.db.commit()
