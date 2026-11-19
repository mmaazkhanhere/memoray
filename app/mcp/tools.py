from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.mcp.auth.service import McpIdentity, OAuthError
from app.memories.application import MemoryApplication
from app.memories.embedding import create_embedding_provider
from app.memories.service import MemoryService
from app.models import RecurrenceFrequency
from app.reminders.application import ReminderApplication
from app.schemas import (
    CancellationScope,
    ReminderListStatus,
    ReminderQuery,
    RescheduleScope,
)


def _auth(scope: str) -> dict:
    return {"type": "oauth2", "scopes": [scope]}


TOOLS = [
    {
        "name": "list_reminders",
        "title": "List reminders",
        "description": "Use this to find the user's reminders before changing one.",
        "inputSchema": {"type": "object", "properties": {
            "title": {"type": ["string", "null"]},
            "start": {"type": ["string", "null"], "description": "Local ISO date or datetime"},
            "end": {"type": ["string", "null"]},
            "occurrence_start": {"type": ["string", "null"], "format": "date-time"},
            "occurrence_end": {"type": ["string", "null"], "format": "date-time"},
            "status": {"type": ["string", "null"], "enum": ["pending", "completed"]},
        }},
        "_meta": {"securitySchemes": [_auth("memoray:reminders.read")]},
    },
    {
        "name": "create_reminder",
        "title": "Create reminder",
        "description": "Create one Telegram-delivered reminder using the user's configured timezone.",
        "inputSchema": {"type": "object", "required": ["title", "date", "time"], "properties": {
            "title": {"type": "string", "minLength": 1},
            "date": {"type": "string", "format": "date"},
            "time": {"type": "string", "description": "HH:MM[:SS] in the user's timezone"},
            "recurring": {"type": "boolean", "default": False},
            "recurrence_frequency": {"type": ["string", "null"], "enum": ["daily", "weekly", "monthly", "yearly", None]},
            "recurrence_interval": {"type": "integer", "minimum": 1, "default": 1},
            "urgent": {"type": "boolean", "default": False, "description": "Use urgent phone-call escalation when explicitly requested."},
            "idempotency_key": {"type": ["string", "null"]},
        }},
        "_meta": {"securitySchemes": [_auth("memoray:reminders.write")]},
    },
    {
        "name": "create_reminders",
        "title": "Create reminders",
        "description": "Create multiple explicit reminders; every item needs title, date, and time.",
        "inputSchema": {"type": "object", "required": ["reminders"], "properties": {
            "reminders": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": ["title", "date", "time"], "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "date": {"type": "string", "format": "date"},
                    "time": {"type": "string"},
                    "recurring": {"type": "boolean", "default": False},
                    "recurrence_frequency": {"type": ["string", "null"], "enum": ["daily", "weekly", "monthly", "yearly", None]},
                    "recurrence_interval": {"type": "integer", "minimum": 1, "default": 1},
                    "urgent": {"type": "boolean", "default": False, "description": "Use urgent phone-call escalation when explicitly requested."},
                },
            }},
            "idempotency_key": {"type": ["string", "null"]},
        }},
        "_meta": {"securitySchemes": [_auth("memoray:reminders.write")]},
    },
    {
        "name": "reschedule_reminder",
        "title": "Reschedule reminder",
        "description": "Mutation only. First resolve one exact reminder ID with list_reminders.",
        "inputSchema": {"type": "object", "required": ["reminder_id", "date", "time", "scope"], "properties": {
            "reminder_id": {"type": "string", "format": "uuid"},
            "occurrence_id": {"type": ["string", "null"], "format": "uuid"},
            "date": {"type": "string", "format": "date"}, "time": {"type": "string"},
            "scope": {"type": "string", "enum": ["series", "occurrence"]},
            "idempotency_key": {"type": ["string", "null"]},
        }},
        "_meta": {"securitySchemes": [_auth("memoray:reminders.write")]},
    },
    {
        "name": "cancel_reminder",
        "title": "Cancel reminder",
        "description": "Mutation only. Use an exact reminder ID and explicit cancellation scope.",
        "inputSchema": {"type": "object", "required": ["reminder_id", "scope"], "properties": {
            "reminder_id": {"type": "string", "format": "uuid"},
            "occurrence_id": {"type": ["string", "null"], "format": "uuid"},
            "scope": {"type": "string", "enum": ["one_time", "occurrence", "series"]},
            "idempotency_key": {"type": ["string", "null"]},
        }},
        "_meta": {"securitySchemes": [_auth("memoray:reminders.write")]},
    },
    {
        "name": "snooze_occurrence",
        "title": "Snooze occurrence",
        "description": "Snooze exactly one delivered occurrence by a positive number of seconds.",
        "inputSchema": {"type": "object", "required": ["reminder_id", "occurrence_id", "duration_seconds"], "properties": {
            "reminder_id": {"type": "string", "format": "uuid"},
            "occurrence_id": {"type": "string", "format": "uuid"},
            "duration_seconds": {"type": "integer", "minimum": 1},
            "idempotency_key": {"type": ["string", "null"]},
        }},
        "_meta": {"securitySchemes": [_auth("memoray:reminders.write")]},
    },
    {
        "name": "complete_delivered_occurrence",
        "title": "Complete delivered reminder",
        "description": "Mark exactly one delivered occurrence as done.",
        "inputSchema": {"type": "object", "properties": {
            "occurrence_id": {"type": ["string", "null"], "format": "uuid"},
            "title": {"type": ["string", "null"]},
            "idempotency_key": {"type": ["string", "null"]},
        }},
        "_meta": {"securitySchemes": [_auth("memoray:reminders.write")]},
    },
    {
        "name": "create_memory",
        "title": "Create memory",
        "description": "Store one clear durable fact only when the user explicitly asks to remember it.",
        "inputSchema": {"type": "object", "required": ["content"], "properties": {
            "content": {"type": "string", "minLength": 1},
            "idempotency_key": {"type": ["string", "null"]},
        }},
        "_meta": {"securitySchemes": [_auth("memoray:memories.write")]},
    },
    {
        "name": "retrieve_memories",
        "title": "Search memories",
        "description": "Search only the user's durable memories; do not invent facts outside the results.",
        "inputSchema": {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 1},
        }},
        "_meta": {"securitySchemes": [_auth("memoray:memories.read")]},
    },
]


def _parse_local_datetime(args: dict[str, Any]) -> datetime:
    return datetime.combine(date.fromisoformat(args["date"]), time.fromisoformat(args["time"]))


def _parse_filter(value: str | None, *, end: bool = False):
    if not value:
        return None
    if len(value) == 10:
        day = date.fromisoformat(value)
        return datetime.combine(day, time.max if end else time.min)
    return datetime.fromisoformat(value)


def _memory_application(db: Session) -> MemoryApplication:
    settings = get_settings()
    provider = create_embedding_provider(settings)
    service = MemoryService(
        db,
        provider,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        candidate_limit=settings.memory_candidate_limit,
        min_similarity=settings.memory_min_similarity,
    )
    return MemoryApplication(service)


def execute(name: str, args: dict[str, Any], identity: McpIdentity, db: Session) -> dict[str, Any]:
    identity.require(
        "memoray:reminders.read"
        if name == "list_reminders"
        else "memoray:memories.read"
        if name == "retrieve_memories"
        else "memoray:memories.write"
        if name == "create_memory"
        else "memoray:reminders.write"
    )
    reminders = ReminderApplication(db, require_telegram=True)
    try:
        if name == "list_reminders":
            query = ReminderQuery(
                title=args.get("title"),
                start=_parse_filter(args.get("start")),
                end=_parse_filter(args.get("end"), end=True),
                occurrence_start=_parse_filter(args.get("occurrence_start")),
                occurrence_end=_parse_filter(args.get("occurrence_end"), end=True),
                status=args.get("status"),
            )
            return {"operation": "retrieved", "count": len(items := reminders.list(identity.user_id, query)), "reminders": items}
        if name == "create_reminder":
            frequency = RecurrenceFrequency(args["recurrence_frequency"]) if args.get("recurrence_frequency") else None
            if args.get("recurring") and frequency is None:
                raise ValueError("recurrence frequency is required for recurring reminders")
            reminder = reminders.create(
                identity.user_id,
                title=args["title"],
                schedule_date=date.fromisoformat(args["date"]),
                schedule_time=time.fromisoformat(args["time"]),
                recurrence_frequency=frequency,
                recurrence_interval=int(args.get("recurrence_interval", 1)),
                is_urgent=bool(args.get("urgent", False)),
            )
            return {"operation": "created", "reminder": reminder}
        if name == "create_reminders":
            created, rejected = [], []
            for item in args.get("reminders", []):
                try:
                    frequency = RecurrenceFrequency(item["recurrence_frequency"]) if item.get("recurrence_frequency") else None
                    if item.get("recurring") and frequency is None:
                        raise ValueError("recurrence frequency is required for recurring reminders")
                    created.append(reminders.create(
                        identity.user_id,
                        title=item["title"],
                        schedule_date=date.fromisoformat(item["date"]),
                        schedule_time=time.fromisoformat(item["time"]),
                        recurrence_frequency=frequency,
                        recurrence_interval=int(item.get("recurrence_interval", 1)),
                        is_urgent=bool(item.get("urgent", False)),
                    ))
                except (KeyError, TypeError, ValueError, LookupError) as exc:
                    rejected.append({"title": item.get("title"), "error": str(exc)})
            return {"operation": "created_batch", "created_count": len(created), "created": created, "rejected": rejected}
        if name == "reschedule_reminder":
            reminder = reminders.reschedule(
                identity.user_id,
                uuid.UUID(args["reminder_id"]),
                schedule=_parse_local_datetime(args),
                scope=RescheduleScope(args["scope"]),
                occurrence_id=uuid.UUID(args["occurrence_id"]) if args.get("occurrence_id") else None,
            )
            return {"operation": "rescheduled", "reminder": reminder}
        if name == "cancel_reminder":
            reminder = reminders.cancel(
                identity.user_id,
                uuid.UUID(args["reminder_id"]),
                scope=CancellationScope(args["scope"]),
                occurrence_id=uuid.UUID(args["occurrence_id"]) if args.get("occurrence_id") else None,
            )
            return {"operation": "canceled", "reminder": reminder}
        if name == "snooze_occurrence":
            reminder = reminders.snooze(
                identity.user_id,
                uuid.UUID(args["reminder_id"]),
                uuid.UUID(args["occurrence_id"]),
                int(args["duration_seconds"]),
            )
            return {"operation": "snoozed", "reminder": reminder}
        if name == "complete_delivered_occurrence":
            return reminders.complete(
                identity.user_id,
                occurrence_id=uuid.UUID(args["occurrence_id"]) if args.get("occurrence_id") else None,
                title=args.get("title"),
            )
        if name == "create_memory":
            return {"operation": "memory_created", "memory": _memory_application(db).create(identity.user_id, args["content"])}
        if name == "retrieve_memories":
            candidates = _memory_application(db).search(identity.user_id, args["query"])
            return {"operation": "memories_retrieved", "count": len(candidates), "candidates": candidates}
        raise ValueError("unsupported MCP tool")
    except (ValueError, LookupError, TypeError) as exc:
        return {"operation": "rejected", "error": str(exc)}


def mcp_result(result: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(result, separators=(",", ":"), default=str)
    return {"content": [{"type": "text", "text": encoded}], "structuredContent": result}
