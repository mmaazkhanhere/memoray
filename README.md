# Memoray

Reminder agent built with FastAPI, SQLAlchemy, Alembic, PostgreSQL, and a provider-agnostic LLM interface.

## Local development

```powershell
uv sync --dev
docker compose up -d db
Copy-Item .env.example .env
uv run alembic upgrade head
uv run uvicorn app.api:app --reload
```

The agent exposes `POST /users/{user_id}/agent` and the underlying reminder operations are also available at `POST /users/{user_id}/reminders`, `GET /users/{user_id}/reminders`, `POST /users/{user_id}/reminders/{reminder_id}/reschedule`, `POST /users/{user_id}/reminders/{reminder_id}/cancel`, and `POST /users/{user_id}/reminders/{reminder_id}/occurrences/{occurrence_id}/snooze`.

The agent can also save durable user facts as memories and retrieve them with pgvector cosine search. Memories have no
schedule and are isolated by user. Saving an unclear fact causes the agent to ask for clarification. Retrieval only
answers from indexed candidates; no match is reported as unknown and multiple matches produce a clarification question.

Memory indexing defaults to Gemini `gemini-embedding-001` and uses `GEMINI_API_KEY`,
`EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, and `EMBEDDING_DIMENSIONS`. The memory service depends only on the
provider-neutral `EmbeddingProvider` interface; adapters can be selected by changing `EMBEDDING_PROVIDER`.
Run `uv run alembic upgrade head` after starting the pgvector-enabled database. If the embedding provider is unavailable,
the memory remains durable with a pending or failed embedding status and can be retried through
`MemoryService.retry_pending`.

Agent requests may include a `conversation_id`. If omitted, the response creates one. Reminder creation details are saved as a draft between turns until title, date, and time are complete. Recurring reminders additionally require `daily`, `weekly`, `monthly`, or `yearly` frequency.

When one message contains multiple complete reminder requests, the agent uses a batch creation tool and returns one conversational response after processing each item.

`POST /users` returns an `access_token`. Send it on reminder and agent requests as:

```text
Authorization: Bearer <access_token>
```

The authenticated token must belong to the `{user_id}` in the URL. Reminder schedules are stored in UTC, displayed in the user's IANA timezone, must be strictly in the future, and cannot duplicate an active pending reminder for the same user, title, and instant.

Rescheduling separates discovery from mutation. The agent uses `list_reminders` with a title substring, then at most one broader fallback, and only mutates after resolving exactly one reminder ID. `reschedule_reminder` accepts only the exact `reminder_id`, a future schedule, and an explicit `scope`; it never searches by title. Recurring reminders require an explicit `scope`: `occurrence` changes only the selected pending occurrence and preserves the series cadence; `series` shifts all future pending occurrences. Narrow `occurrence_start`/`occurrence_end` listing filters can resolve a requested recurring occurrence without loading its full history. Snoozing requires a single `notified` occurrence and a positive `duration_seconds`, then returns it to `pending` with `next_due_at` set to the current time plus that duration. Reminders use a linked, valid delivery destination owned by the same user.

Cancellation is also discovery-first: the agent lists pending reminders, asks for clarification when targets or scope are ambiguous, and then calls the mutation-only `cancel_reminder` tool with exact IDs. `one_time` cancels a non-recurring reminder, `occurrence` skips one pending recurring occurrence while continuing the series, and `series` deactivates the entire recurring reminder.

The agent uses a bounded sequence of LLM turns for discovery, mutation, and the final conversational response. Database writes and validation remain deterministic.
