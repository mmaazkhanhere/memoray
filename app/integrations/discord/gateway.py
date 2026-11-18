from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.agent.factory import create_agent
from app.channels.contracts import IncomingMessage
from app.channels.service import ChannelMessageService
from app.db import SessionLocal
from app.integrations.discord.client import DiscordClient
from app.integrations.discord.errors import DiscordDeliveryError
from app.models import ChannelKind, OutboundMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingReply:
    id: object
    channel_id: str
    content: str


class DiscordGateway:
    """Gateway-facing adapter; business logic remains in ChannelMessageService."""

    def __init__(self, *, token: str, client: DiscordClient | None = None):
        self.token = token
        self.client = client or DiscordClient(bot_token=token)

    def process_message(
        self,
        *,
        message_id: str,
        author_id: str,
        channel_id: str,
        content: str,
        display_name: str | None,
    ) -> list[PendingReply]:
        incoming = IncomingMessage(
            channel=ChannelKind.discord.value,
            external_update_id=message_id,
            external_user_id=author_id,
            external_chat_id=channel_id,
            text=content,
            display_name=display_name,
        )
        db = SessionLocal()
        try:
            records = ChannelMessageService(db, create_agent).process(
                incoming, create_account=False
            )
            return [PendingReply(row.id, row.external_chat_id, row.content) for row in records]
        finally:
            db.close()

    def pending(self) -> list[PendingReply]:
        db = SessionLocal()
        try:
            rows = list(
                db.scalars(
                    select(OutboundMessage)
                    .where(
                        OutboundMessage.channel == ChannelKind.discord,
                        OutboundMessage.sent_at.is_(None),
                    )
                    .order_by(OutboundMessage.created_at.asc())
                )
            )
            return [PendingReply(row.id, row.external_chat_id, row.content) for row in rows]
        finally:
            db.close()

    def send_reply(self, reply: PendingReply) -> None:
        try:
            self.client.send_message(reply.channel_id, reply.content)
        except DiscordDeliveryError as exc:
            db = SessionLocal()
            try:
                ChannelMessageService(db, create_agent).mark_failed(reply.id, str(exc))
            finally:
                db.close()
            raise
        db = SessionLocal()
        try:
            ChannelMessageService(db, create_agent).mark_sent(reply.id)
        finally:
            db.close()


class DiscordGatewayRunner:
    """Minimal Gateway loop; REST and domain work stay in separate adapters."""

    gateway_url = "wss://gateway.discord.gg/?v=10&encoding=json"
    # GUILDS, DIRECT_MESSAGES, and MESSAGE_CONTENT.
    intents = 1 | 1 << 12 | 1 << 15

    def __init__(self, gateway: DiscordGateway):
        self.gateway = gateway

    async def run(self) -> None:
        from websockets.asyncio.client import connect

        while True:
            try:
                async with connect(self.gateway_url, max_size=8 * 1024 * 1024) as socket:
                    hello = json.loads(await socket.recv())
                    heartbeat_interval = float(hello["d"]["heartbeat_interval"]) / 1000
                    await socket.send(
                        json.dumps(
                            {
                                "op": 2,
                                "d": {
                                    "token": self.gateway.token,
                                    "intents": self.intents,
                                    "properties": {"os": "linux", "browser": "memoray", "device": "memoray"},
                                },
                            }
                        )
                    )
                    heartbeat = asyncio.create_task(self._heartbeat(socket, heartbeat_interval))
                    drain = asyncio.create_task(self._drain())
                    try:
                        async for raw in socket:
                            payload = json.loads(raw)
                            if payload.get("op") == 11:
                                continue
                            if payload.get("t") == "MESSAGE_CREATE":
                                await self._message(payload.get("d") or {})
                    finally:
                        heartbeat.cancel()
                        drain.cancel()
                        await asyncio.gather(heartbeat, drain, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Discord Gateway connection failed; reconnecting")
                await asyncio.sleep(5)

    async def _heartbeat(self, socket, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await socket.send(json.dumps({"op": 1, "d": None}))

    async def _drain(self) -> None:
        while True:
            for reply in self.gateway.pending():
                try:
                    await asyncio.to_thread(self.gateway.send_reply, reply)
                except DiscordDeliveryError:
                    logger.exception("Discord outbox delivery failed", extra={"message_id": str(reply.id)})
            await asyncio.sleep(15)

    async def _message(self, data: dict) -> None:
        author = data.get("author") or {}
        if author.get("bot") or data.get("guild_id") or not str(data.get("content") or "").strip():
            return
        replies = await asyncio.to_thread(
            self.gateway.process_message,
            message_id=str(data.get("id") or ""),
            author_id=str(author.get("id") or ""),
            channel_id=str(data.get("channel_id") or ""),
            content=str(data.get("content") or ""),
            display_name=str(author.get("global_name") or author.get("username") or "") or None,
        )
        for reply in replies:
            try:
                await asyncio.to_thread(self.gateway.send_reply, reply)
            except DiscordDeliveryError:
                logger.exception("Discord reply delivery failed", extra={"message_id": str(reply.id)})
