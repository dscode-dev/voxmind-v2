from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = Field(default="prod", description="Environment name")

    # Auth
    api_key: str = Field(..., alias="CONTROL_PLANE_API_KEY")

    # K8s
    namespace: str = Field(default="voxmind-v2", alias="VOXMIND_NAMESPACE")
    worker_job_image: str = Field(..., alias="WORKER_JOB_IMAGE")
    worker_job_ttl_seconds_after_finished: int = Field(default=3600, alias="WORKER_JOB_TTL")
    worker_job_active_deadline_seconds: int = Field(default=21600, alias="WORKER_JOB_DEADLINE")
    worker_cpu_request: str = Field(default="1000m", alias="WORKER_CPU_REQUEST")
    worker_cpu_limit: str = Field(default="2000m", alias="WORKER_CPU_LIMIT")
    worker_mem_request: str = Field(default="2Gi", alias="WORKER_MEM_REQUEST")
    worker_mem_limit: str = Field(default="4Gi", alias="WORKER_MEM_LIMIT")

    # Observability
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Telegram optional
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")

settings = Settings()
