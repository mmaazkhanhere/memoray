def _optional_string(format_name: str | None = None) -> dict:
    value = {"type": "string"}
    if format_name:
        value["format"] = format_name
    return {"anyOf": [value, {"type": "null"}]}


def _optional_date_or_datetime() -> dict:
    return {
        "anyOf": [
            {"type": "string", "format": "date-time"},
            {"type": "string", "format": "date"},
            {"type": "null"},
        ]
    }


def _optional_datetime() -> dict:
    return {"anyOf": [{"type": "string", "format": "date-time"}, {"type": "null"}]}


RECURRENCE_FREQUENCIES = ["daily", "weekly", "monthly", "yearly"]


UPDATE_REMINDER_DRAFT_TOOL = {
    "type": "function",
    "function": {
        "name": "update_reminder_draft",
        "description": (
            "Save any reminder details the user has provided. Use this when title, date, or time "
            "is still missing; do not invent missing values."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": _optional_string(),
                "date": _optional_string("date"),
                "time": _optional_string(),
                "recurring": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
                "recurrence_interval": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]},
                "recurrence_frequency": {
                    "anyOf": [
                        {"type": "string", "enum": RECURRENCE_FREQUENCIES},
                        {"type": "null"},
                    ]
                },
            },
            "additionalProperties": False,
        },
    },
}

CREATE_REMINDER_TOOL = {
    "type": "function",
    "function": {
        "name": "create_reminder",
        "description": (
            "Create a reminder only when the draft has title, date, and time. "
            "Recurring reminders also require a frequency."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": _optional_string(),
                "date": _optional_string("date"),
                "time": _optional_string(),
                "recurring": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
                "recurrence_interval": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]},
                "recurrence_frequency": {
                    "anyOf": [
                        {"type": "string", "enum": RECURRENCE_FREQUENCIES},
                        {"type": "null"},
                    ]
                },
            },
            "additionalProperties": False,
        },
    },
}

CREATE_REMINDERS_TOOL = {
    "type": "function",
    "function": {
        "name": "create_reminders",
        "description": (
            "Create multiple reminders from one user message. Use this when the message contains "
            "two or more distinct reminder requests. Every item must have title, date, and time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reminders": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "date": {"type": "string", "format": "date"},
                            "time": {"type": "string"},
                            "recurring": {"type": "boolean"},
                            "recurrence_interval": {"type": "integer", "minimum": 1},
                            "recurrence_frequency": {
                                "anyOf": [
                                    {"type": "string", "enum": RECURRENCE_FREQUENCIES},
                                    {"type": "null"},
                                ]
                            },
                        },
                        "required": ["title", "date", "time"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["reminders"],
            "additionalProperties": False,
        },
    },
}

LIST_REMINDERS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_reminders",
        "description": (
            "List reminders. Questions such as 'do I have anything today?' mean list reminders "
            "for today's date in the user's timezone. Use occurrence_start and occurrence_end "
            "to resolve a narrow pending occurrence window for a recurring reminder without "
            "loading its full history."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": _optional_string(),
                "start": _optional_date_or_datetime(),
                "end": _optional_date_or_datetime(),
                "occurrence_start": _optional_datetime(),
                "occurrence_end": _optional_datetime(),
                "status": {
                    "anyOf": [
                        {"type": "string", "enum": ["pending", "completed"]},
                        {"type": "null"},
                    ]
                },
            },
            "additionalProperties": False,
        },
    },
}

RESCHEDULE_REMINDER_TOOL = {
    "type": "function",
    "function": {
        "name": "reschedule_reminder",
        "description": (
            "Mutation only: move one already-resolved active reminder to a strictly future time. "
            "The exact reminder_id is required; never use this tool to search by title. Use "
            "scope=occurrence for one pending occurrence and scope=series only when changing the "
            "entire recurring schedule. occurrence_id is required for occurrence scope."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string", "format": "uuid"},
                "occurrence_id": _optional_string("uuid"),
                "schedule": {"type": "string", "format": "date-time"},
                "scope": {
                    "type": "string",
                    "enum": ["series", "occurrence"],
                    "description": "Use occurrence for a named date such as today/tomorrow; use series only when explicitly requested.",
                },
            },
            "required": ["reminder_id", "schedule", "scope"],
            "additionalProperties": False,
        },
    },
}

CANCEL_REMINDER_TOOL = {
    "type": "function",
    "function": {
        "name": "cancel_reminder",
        "description": (
            "Mutation only: cancel an already-resolved reminder. The exact reminder_id is required; "
            "never use this tool to search by title. Use one_time for a non-recurring reminder, "
            "occurrence for one pending recurring occurrence with its exact occurrence_id, and "
            "series only when the user explicitly wants the entire recurring series stopped."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string", "format": "uuid"},
                "occurrence_id": _optional_string("uuid"),
                "scope": {"type": "string", "enum": ["one_time", "occurrence", "series"]},
            },
            "required": ["reminder_id", "scope"],
            "additionalProperties": False,
        },
    },
}

SNOOZE_OCCURRENCE_TOOL = {
    "type": "function",
    "function": {
        "name": "snooze_occurrence",
        "description": "Snooze exactly one occurrence that has already been delivered; duration is required and positive.",
        "parameters": {
            "type": "object",
            "properties": {
                "reminder_id": _optional_string("uuid"),
                "title": _optional_string(),
                "occurrence_id": _optional_string("uuid"),
                "duration_seconds": {"type": "integer", "minimum": 1},
            },
            "required": ["duration_seconds"],
            "additionalProperties": False,
        },
    },
}

CREATE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "create_memory",
        "description": (
            "Store one durable fact only when the user explicitly asks you to remember it. "
            "The fact must be clear and complete; never infer or fill in missing details. "
            "Memories are not reminders and have no schedule. If the request is ambiguous, ask "
            "a clarification question instead of calling this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {"content": {"type": "string", "minLength": 1}},
            "required": ["content"],
            "additionalProperties": False,
        },
    },
}

RETRIEVE_MEMORIES_TOOL = {
    "type": "function",
    "function": {
        "name": "retrieve_memories",
        "description": (
            "Retrieve durable facts that may answer the user's question. Always use this before "
            "answering a question about something the user asked you to remember. Only facts in "
            "the returned candidates may be used; never invent a missing fact."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

TOOLS = [
    UPDATE_REMINDER_DRAFT_TOOL,
    CREATE_REMINDER_TOOL,
    CREATE_REMINDERS_TOOL,
    LIST_REMINDERS_TOOL,
    RESCHEDULE_REMINDER_TOOL,
    CANCEL_REMINDER_TOOL,
    SNOOZE_OCCURRENCE_TOOL,
    CREATE_MEMORY_TOOL,
    RETRIEVE_MEMORIES_TOOL,
]

