def _optional_string(format_name: str | None = None) -> dict:
    value = {"type": "string"}
    if format_name:
        value["format"] = format_name
    return {"anyOf": [value, {"type": "null"}]}


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
            "for today's date in the user's timezone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": _optional_string(),
                "start": _optional_string("date-time"),
                "end": _optional_string("date-time"),
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

TOOLS = [UPDATE_REMINDER_DRAFT_TOOL, CREATE_REMINDER_TOOL, CREATE_REMINDERS_TOOL, LIST_REMINDERS_TOOL]

