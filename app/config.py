from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://memoray:memoray@localhost:5432/memoray"
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
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
    telegram_webhook_secret: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

