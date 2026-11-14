CREATE_REMINDER_TOOL = {
    "type": "function",
    "function": {
        "name": "create_reminder",
        "description": "Create a reminder. Ask for missing title or date/time before calling.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Reminder title"},
                "scheduled_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": "ISO 8601 date-time resolved in the user's timezone",
                },
            },
            "required": ["title", "scheduled_at"],
            "additionalProperties": False,
        },
    },
}

LIST_REMINDERS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_reminders",
        "description": "List reminders; with no filters returns all reminders for the user.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "start": {
                    "anyOf": [{"type": "string", "format": "date-time"}, {"type": "null"}]
                },
                "end": {
                    "anyOf": [{"type": "string", "format": "date-time"}, {"type": "null"}]
                },
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

TOOLS = [CREATE_REMINDER_TOOL, LIST_REMINDERS_TOOL]

