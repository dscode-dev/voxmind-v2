from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )

    # ======================================
    # Core Pipeline Configuration
    # ======================================
    video_url: str = Field(..., alias="VIDEO_URL")
    pipeline_mode: str = Field(default="v2", alias="PIPELINE_MODE")
    pipeline_stage: str = Field(default="prepare", alias="PIPELINE_STAGE")  # prepare | finalize
    work_dir: str = Field(default="/work", alias="WORK_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    WORKER_JOB_IMAGE: str | None = None

    # ======================================
    # Heuristic Pipeline Features
    # ======================================
    enable_candidate_scoring: bool = Field(
        default=False,
        alias="ENABLE_CANDIDATE_SCORING"
    )

    enable_hook_detector: bool = Field(
        default=False,
        alias="ENABLE_HOOK_DETECTOR"
    )

    # ======================================
    # LLM Configuration (Optional / Future)
    # ======================================
    llm_mode: str = Field(default="mock", alias="LLM_MODE")  # mock | real | manual

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    openai_temperature: float = Field(default=0.3, alias="OPENAI_TEMPERATURE")
    openai_max_tokens: int = Field(default=800, alias="OPENAI_MAX_TOKENS")
    llm_max_chars: int = Field(default=12000, alias="LLM_MAX_CHARS")

    # ======================================
    # ASR (CPU Optimized)
    # ======================================
    asr_model_size: str = Field(default="medium", alias="ASR_MODEL_SIZE")
    asr_language: str = Field(default="pt", alias="ASR_LANGUAGE")
    asr_compute_type: str = Field(default="int8", alias="ASR_COMPUTE_TYPE")
    asr_beam_size: int = Field(default=5, alias="ASR_BEAM_SIZE")
    asr_vad_filter: bool = Field(default=True, alias="ASR_VAD_FILTER")

    # ======================================
    # Telegram Integration
    # ======================================
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")


settings = Settings()