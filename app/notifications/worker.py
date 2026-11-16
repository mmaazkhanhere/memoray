from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from app.db import SessionLocal
from app.models import DeliveryChannel, NotificationDelivery
from app.notifications.service import NotificationService
from app.integrations.telegram.adapter import (
    TelegramBotClient,
    TelegramDeliveryError,
)

logger = logging.getLogger(__name__)


class DeliveryAdapter(Protocol):
    def send(self, notification: NotificationDelivery) -> None: ...


@dataclass(frozen=True)
class WorkerConfig:
    batch_size: int = 100
    lease_seconds: int = 60
    poll_interval_seconds: float = 1.0
    max_attempts: int = 3
    uncertain_retry_limit: int = 2
    max_delay_seconds: int = 300


class TelegramDeliveryAdapter:
    def __init__(self, token: str):
        self.client = TelegramBotClient(token)

    def send(self, notification: NotificationDelivery) -> None:
        self.client.send_message(
            notification.destination_snapshot,
            notification.content,
            request_phone=notification.request_phone,
            idempotency_key=notification.provider_idempotency_key,
        )


class NotificationWorker:
    def __init__(
        self,
        *,
        session_factory=SessionLocal,
        adapters: dict[DeliveryChannel, DeliveryAdapter] | None = None,
        worker_id: str | None = None,
        config: WorkerConfig | None = None,
    ):
        self.session_factory = session_factory
        self.adapters = adapters or {}
        self.worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self.config = config or WorkerConfig()

    def run_once(self) -> int:
        recovery_db = self.session_factory()
        try:
            NotificationService(recovery_db).recover_expired()
        finally:
            recovery_db.close()

        claim_db = self.session_factory()
        try:
            claimed = NotificationService(claim_db).claim_due(
                self.worker_id,
                limit=self.config.batch_size,
                lease_seconds=self.config.lease_seconds,
            )
            claimed_data = [
                (
                    row.id,
                    row.channel,
                    row.destination_snapshot,
                    row.content,
                    row.provider_idempotency_key,
                )
                for row in claimed
            ]
        finally:
            claim_db.close()

        for notification_id, channel, destination, content, key in claimed_data:
            # Re-load the row in the sending transaction's session so adapters
            # never depend on an object that was expired by commit.
            send_db = self.session_factory()
            try:
                row = send_db.get(NotificationDelivery, notification_id)
                if row is None:
                    continue
                adapter = self.adapters.get(channel)
                if adapter is None:
                    raise TelegramDeliveryError(
                        f"no delivery adapter configured for channel {channel.value}",
                        classification="permanent",
                    )
                adapter.send(row)
            except TelegramDeliveryError as exc:
                self._finalize(send_db, notification_id, exc)
            except (TimeoutError, ConnectionError, OSError) as exc:
                self._finalize(send_db, notification_id, exc, classification="unknown")
            except Exception as exc:  # provider adapters must not kill the worker loop
                logger.exception("notification delivery failed", extra={"notification_id": str(notification_id)})
                self._finalize(send_db, notification_id, exc)
            else:
                NotificationService(send_db).finalize(
                    notification_id,
                    self.worker_id,
                    classification="success",
                    max_attempts=self.config.max_attempts,
                    uncertain_retry_limit=self.config.uncertain_retry_limit,
                    max_delay_seconds=self.config.max_delay_seconds,
                )
            finally:
                send_db.close()
        return len(claimed_data)

    def _finalize(self, db, notification_id, exc, classification: str | None = None) -> None:
        error = str(exc)
        classification = classification or getattr(exc, "classification", "retryable")
        NotificationService(db).finalize(
            notification_id,
            self.worker_id,
            classification=classification,
            error=error,
            retry_after=getattr(exc, "retry_after", None),
            invalidate_identity=getattr(exc, "invalidate_identity", False),
            max_attempts=self.config.max_attempts,
            uncertain_retry_limit=self.config.uncertain_retry_limit,
            max_delay_seconds=self.config.max_delay_seconds,
        )

    def run_forever(self) -> None:
        while True:
            processed = self.run_once()
            if processed == 0:
                time.sleep(self.config.poll_interval_seconds)


def main() -> None:
    from app.config import get_settings

    settings = get_settings()
    adapters: dict[DeliveryChannel, DeliveryAdapter] = {}
    if settings.telegram_bot_token:
        adapters[DeliveryChannel.telegram] = TelegramDeliveryAdapter(settings.telegram_bot_token)
    NotificationWorker(adapters=adapters).run_forever()
