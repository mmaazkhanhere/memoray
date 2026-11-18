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

Run the reminder delivery worker as a separate process/container:

```powershell
uv run reminder-worker
```

Reminder sends are persisted in PostgreSQL before their due time. The worker
claims due rows with leased FOR UPDATE SKIP LOCKED transactions, sends outside
the transaction, and conditionally records sent, failed, or unknown.
Restarting the API or worker does not discard pending notifications.

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

## Optional Google Calendar integration

Google Calendar is an optional outbound projection of reminder occurrences. Configure
`GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`,
`GOOGLE_CALENDAR_REDIRECT_URI` (for example,
`https://your-host/integrations/google-calendar/oauth/callback`), and a Fernet
`GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY`, then run `uv run alembic upgrade head`.
The Telegram command `/connect_calendar` (also available through the authenticated
connect endpoint) starts Google OAuth. Users can skip it and continue using every
reminder operation normally.

Reminder changes are written to a durable outbox and synchronized by the existing reminder worker:

```powershell
uv run reminder-worker
```

The integration creates one timed Calendar event per active reminder occurrence. Create,
reschedule, snooze, cancel, and done operations are projected asynchronously. Calendar
events use a 15-minute default duration configured by
`GOOGLE_CALENDAR_EVENT_DURATION_MINUTES`; Memoray remains the source of truth.

## Telegram channel

Telegram is an adapter around the channel-neutral onboarding and agent services. Configure
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET`, then register Telegram's webhook URL as
`https://<host>/webhooks/telegram` with the matching secret token header.

The first `/start` creates one account for the Telegram sender and collects any missing name and IANA timezone.
The user may share Telegram's native contact or skip phone linking. Phone linking is optional for Telegram use but
required for future live-call delivery. Sender identity, phone ownership, and Telegram update IDs are protected by
database constraints and idempotent processing; a Telegram sender can never resolve another sender's account.
If phone linking is skipped, the user can later send `/link phone` and share Telegram's contact, or share the contact
directly. Sending `/cancel` while the link prompt is active leaves phone linking disabled.

Telegram voice notes are validated, downloaded through the Telegram Bot API, and transcribed through the configured
provider-neutral speech-to-text interface. Groq Whisper is the default adapter (`STT_PROVIDER=groq` and
`STT_MODEL=whisper-large-v3-turbo`; `STT_API_KEY` may be used, otherwise `GROQ_API_KEY` is reused). The transcript is processed through the same channel and agent path as a typed
message. Empty or failed transcription produces a text retry response and does not mutate reminders or memories.
The Telegram update ID is claimed before media or STT work, so replaying a voice update does not download, transcribe,
or execute it again. Voice command replies are sent synchronously as Telegram text; scheduled reminder delivery
remains the responsibility of the separate notification worker.

## Discord channel

Discord is an optional second conversational adapter. Configure
`DISCORD_BOT_TOKEN`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, and
`DISCORD_REDIRECT_URI`, run the API service, and register the
redirect URI in the Discord Developer Portal. The bot needs Gateway access to
direct messages and message content.

From a completed Telegram account, send `/connect_discord` (or `/connect discord`).
Memoray returns a single-use Discord authorization URL valid for ten minutes.
The callback links the authorized Discord user to the existing Memoray user and
opens their bot DM; it never creates a second Memoray account. After linking,
send the bot a direct message to create and manage the same memories and
reminders available on Telegram. Discord server messages are ignored in this
initial adapter; scheduled Discord reminder notifications use the linked DM
destination and the existing durable notification worker. When
`DISCORD_BOT_TOKEN` is configured, the API service starts the Discord Gateway
loop automatically; no additional service is required.

## Vapi phone channel

Vapi is an adapter over the same reminder and memory services. Configure the required `VAPI_WEBHOOK_SECRET`, run
`uv run alembic upgrade head`, and configure the Vapi phone number with:

```text
https://<host>/webhooks/vapi
```

Attach a Vapi custom Bearer credential containing `VAPI_WEBHOOK_SECRET`. The number must use an
`assistant-request` flow. Memoray authenticates the incoming E.164 caller against an active linked phone identity,
then returns the configured Riley assistant ID. Missing, unknown, inactive, malformed, or failed identity lookups
fail closed and never start the AI conversation. Assistant selection performs only a local, indexed lookup within
Vapi's fixed 7.5-second assistant-request deadline.

The authenticated `assistant-request` selects the saved Riley assistant configured by `VAPI_ASSISTANT_ID` and sends
per-call `assistantOverrides.variableValues` for `user_name`, `user_timezone`, and `current_local_time`. Use these
variables in Riley's prompt as `{{user_name}}`, `{{user_timezone}}`, and `{{current_local_time}}`. Riley's saved prompt,
voice, and function schemas remain managed in Vapi (or synchronized explicitly from the repository). Its tool calls
are sent to the same `/webhooks/vapi` endpoint. A
Vapi call gets one durable conversation and one tool-result cache, so Vapi retries cannot create a second reminder.
Call-created reminders require a linked Telegram identity and are scheduled through the existing Telegram notification
worker. The call context stores an unambiguous current reminder, allowing a correction such as “make it half an hour”
to reschedule the same reminder; ambiguous targets are rejected for clarification. Memory writes and retrieval remain
user-scoped and immediately available to Telegram and phone channels.

The API logs Vapi lifecycle events as `vapi.*` (request receipt, assistant selection/rejection, tool result, and end
report) without logging authorization headers, phone numbers, transcripts, or tool arguments. Set `LOG_LEVEL=INFO`
and inspect the API process logs alongside the Vapi Dashboard **Webhook logs**. If no `vapi.received` line appears,
Vapi did not reach this deployment: check the phone number's Server URL, custom Bearer credential, public HTTPS
reachability, and the selected Vapi project.

## Remote MCP plugin

Memoray exposes the same canonical reminder and memory store as an authenticated MCP server. Set
`MCP_ENABLED=true`, configure `MCP_PUBLIC_URL` to the public HTTPS origin, set a unique `MCP_TOKEN_SECRET`, and
configure `TELEGRAM_BOT_USERNAME`. The MCP endpoint is `/mcp` (with an SSE-compatible `/mcp/sse` transport).

The first ChatGPT connection opens an OAuth page that asks the user to approve the connection from their existing
Telegram account. MCP never accepts a user ID from the client; the OAuth token identifies the linked Memoray user.
Reminder mutations require exact IDs and use the linked Telegram identity for notification delivery. Google Calendar
is not required: reminder changes remain durable in Memoray and are projected only when a calendar connection exists.

`VAPI_VOICE_PROVIDER` and `VAPI_VOICE_ID` are used when synchronizing Riley from the repository. The request deadline
defaults to six seconds.

### Sync Riley from the repository

Use the explicit sync command after changing the shared agent tools or prompt. It updates Riley through Vapi's
Assistant API; it does not run automatically when the app starts.

```powershell
$env:VAPI_API_KEY = "your-vapi-private-api-key"
$env:VAPI_ASSISTANT_ID = "your-riley-assistant-id"
uv run sync-vapi-assistant
```

To also set the assistant-level server configuration, provide the public webhook URL and the ID of the existing
Vapi custom credential:

```powershell
$env:VAPI_SERVER_URL = "https://your-host.example/webhooks/vapi"
$env:VAPI_CREDENTIAL_ID = "your-vapi-credential-id"
uv run sync-vapi-assistant
```

Without those optional variables, the command leaves Riley's current server URL and credential untouched while
replacing its prompt, first message, voice settings, and all Memoray function schemas.
