from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://memoray:memoray@localhost:5432/memoray"
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    stt_provider: str = "groq"
    stt_model: str = "whisper-large-v3-turbo"
    stt_api_key: str | None = None
    telegram_voice_max_duration_seconds: int = 120
    telegram_voice_max_bytes: int = 10 * 1024 * 1024
    embedding_provider: str = "gemini"
    embedding_api_key: str | None = None
    gemini_api_key: str | None = None
    embedding_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = 1536
    memory_candidate_limit: int = 5
    memory_min_similarity: float = 0.75
    app_env: str = "development"
    auth_token_secret: str = "change-this-development-secret"
    telegram_bot_token: str | None = None
    telegram_bot_username: str | None = None
    telegram_webhook_secret: str | None = None
    discord_bot_token: str | None = None
    discord_client_id: str | None = None
    discord_client_secret: str | None = None
    discord_redirect_uri: str | None = None
    discord_link_ttl_seconds: int = Field(default=600, gt=0)
    vapi_webhook_secret: str | None = None
    vapi_assistant_id: str | None = None
    vapi_outbound_assistant_id: str | None = None
    vapi_phone_number_id: str | None = None
    vapi_api_key: str | None = None
    vapi_server_url: str | None = None
    vapi_credential_id: str | None = None
    vapi_voice_provider: str | None = None
    vapi_voice_id: str | None = None
    vapi_voice_model: str | None = None
    vapi_assistant_response_mode: str = "message_response"
    vapi_assistant_request_deadline_seconds: float = 6.0
    urgent_reminders_enabled: bool = False
    urgent_call_retry_delay_seconds: int = Field(default=30, gt=0)
    urgent_call_lease_seconds: int = Field(default=90, gt=0)
    google_calendar_client_id: str | None = None
    google_calendar_client_secret: str | None = None
    google_calendar_redirect_uri: str | None = None
    google_calendar_token_encryption_key: str | None = None
    google_calendar_event_duration_minutes: int = Field(default=15, gt=0, le=1440)
    google_calendar_worker_id: str = "google-calendar-worker"
    google_calendar_job_lease_seconds: int = Field(default=120, gt=0)
    google_calendar_oauth_state_ttl_seconds: int = Field(default=600, gt=0)
    mcp_enabled: bool = False
    mcp_public_url: str = "http://localhost:8000"
    mcp_issuer: str | None = None
    mcp_token_secret: str = "change-this-development-mcp-secret"
    mcp_access_token_ttl_seconds: int = Field(default=3600, gt=0)
    mcp_refresh_token_ttl_seconds: int = Field(default=2592000, gt=0)
    mcp_link_ttl_seconds: int = Field(default=600, gt=0)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

