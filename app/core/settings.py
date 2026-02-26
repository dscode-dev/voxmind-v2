from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_chat_id: str | None = Field(default=None, alias="TELEGRAM_ALLOWED_CHAT_ID")

    # LLM
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")

    llm_model_segmentation: str = Field(default="gpt-4o-mini", alias="LLM_MODEL_SEGMENTATION")
    llm_model_scoring: str = Field(default="gpt-4o", alias="LLM_MODEL_SCORING")
    llm_model_copy: str = Field(default="gpt-4o-mini", alias="LLM_MODEL_COPY")

    llm_timeout_seconds: int = Field(default=45, alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=3, alias="LLM_MAX_RETRIES")
    llm_max_input_chars: int = Field(default=20000, alias="LLM_MAX_INPUT_CHARS")

    # Cache
    cache_enabled: bool = Field(default=True, alias="CACHE_ENABLED")
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    cache_ttl_seconds: int = Field(default=60 * 60 * 24 * 7, alias="CACHE_TTL_SECONDS")

    # Dev
    mock_llm: bool = Field(default=False, alias="MOCK_LLM")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


def get_settings() -> Settings:
    return Settings()
