from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    video_url: str = Field(..., alias="VIDEO_URL")
    pipeline_mode: str = Field(default="v2", alias="PIPELINE_MODE")

    # ASR preset (CPU)
    asr_model_size: str = Field(default="medium", alias="ASR_MODEL_SIZE")
    asr_language: str = Field(default="pt", alias="ASR_LANGUAGE")
    asr_compute_type: str = Field(default="int8", alias="ASR_COMPUTE_TYPE")
    asr_beam_size: int = Field(default=5, alias="ASR_BEAM_SIZE")
    asr_vad_filter: bool = Field(default=True, alias="ASR_VAD_FILTER")

    work_dir: str = Field(default="/work", alias="WORK_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")

settings = Settings()
