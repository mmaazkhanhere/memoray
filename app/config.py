from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://memoray:memoray@localhost:5432/memoray"
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    app_env: str = "development"
    auth_token_secret: str = "change-this-development-secret"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

