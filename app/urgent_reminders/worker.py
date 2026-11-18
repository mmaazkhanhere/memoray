from __future__ import annotations

import time
import uuid

from app.db import SessionLocal
from app.config import get_settings
from app.models import User, UrgentDeliveryStatus
from app.urgent_reminders.service import UrgentReminderService
from app.urgent_reminders.vapi_client import VapiOutboundClient, VapiOutboundError


class UrgentReminderWorker:
    def __init__(
        self,
        *,
        client: VapiOutboundClient,
        session_factory=SessionLocal,
        worker_id: str | None = None,
    ):
        self.client = client
        self.session_factory = session_factory
        self.worker_id = worker_id or f"urgent-worker-{uuid.uuid4()}"

    def run_once(self) -> int:
        recovery_db = self.session_factory()
        try:
            UrgentReminderService(recovery_db).recover_expired()
        finally:
            recovery_db.close()
        db = self.session_factory()
        try:
            settings = get_settings()
            claimed = UrgentReminderService(
                db, retry_delay_seconds=settings.urgent_call_retry_delay_seconds
            ).claim_due(self.worker_id, lease_seconds=settings.urgent_call_lease_seconds)
            payloads = []
            for delivery, attempt in claimed:
                user = db.get(User, delivery.reminder.user_id) if delivery.reminder else None
                payloads.append(
                    (
                        attempt.correlation_key,
                        delivery.phone_snapshot,
                        user.name if user else "there",
                        delivery.reminder.title
                        if delivery.reminder
                        else "your urgent reminder",
                    )
                )
        finally:
            db.close()
        processed = 0
        for correlation_key, phone, user_name, title in payloads:
            send_db = self.session_factory()
            try:
                attempt = UrgentReminderService(send_db).find_attempt(correlation_key=correlation_key)
                if attempt is None or attempt.delivery.status != UrgentDeliveryStatus.calling:
                    continue
                try:
                    call_id = self.client.start_call(
                        phone_number=phone,
                        correlation_key=correlation_key,
                        user_name=user_name,
                        reminder_text=f"Urgent reminder: {title}. Please complete this task.",
                    )
                except (VapiOutboundError, TimeoutError, ConnectionError, OSError) as exc:
                    UrgentReminderService(
                        send_db, retry_delay_seconds=get_settings().urgent_call_retry_delay_seconds
                    ).record_dispatch_failure(correlation_key, str(exc))
                else:
                    UrgentReminderService(send_db).record_call_started(correlation_key, call_id)
                processed += 1
            finally:
                send_db.close()
        return processed

    def run_forever(self, poll_interval_seconds: float = 1.0) -> None:
        while True:
            if self.run_once() == 0:
                time.sleep(poll_interval_seconds)
