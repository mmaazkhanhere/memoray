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

The agent exposes `POST /users/{user_id}/agent` and the underlying reminder operations are also available at `POST /users/{user_id}/reminders` and `GET /users/{user_id}/reminders`.

`POST /users` returns an `access_token`. Send it on reminder and agent requests as:

```text
Authorization: Bearer <access_token>
```

The authenticated token must belong to the `{user_id}` in the URL. Reminder schedules are stored in UTC, displayed in the user's IANA timezone, must be strictly in the future, and cannot duplicate an active pending reminder for the same user, title, and instant.

The agent uses two LLM turns: the first selects a reminder tool, and the second turns the tool's structured result into the conversational response. Database writes and validation remain deterministic.
