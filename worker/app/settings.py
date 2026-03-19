from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ======================================
    # Core Pipeline Configuration
    # ======================================

    video_url: str | None = Field(default=None, alias="VIDEO_URL")

    pipeline_mode: str = Field(default="v2", alias="PIPELINE_MODE")

    pipeline_stage: str = Field(
        default="prepare",
        alias="PIPELINE_STAGE"
    )  # prepare | finalize

    work_dir: str = Field(default="/work", alias="WORK_DIR")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_json: bool = Field(default=True, alias="LOG_JSON")
    keep_workdir_on_failure: bool = Field(
        default=True,
        alias="KEEP_WORKDIR_ON_FAILURE"
    )

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
    # LLM Configuration
    # ======================================

    llm_mode: str = Field(
        default="mock",
        alias="LLM_MODE"
    )

    openai_api_key: str | None = Field(
        default=None,
        alias="OPENAI_API_KEY"
    )

    openai_model: str = Field(
        default="gpt-4o-mini",
        alias="OPENAI_MODEL"
    )

    openai_temperature: float = Field(
        default=0.3,
        alias="OPENAI_TEMPERATURE"
    )

    openai_max_tokens: int = Field(
        default=800,
        alias="OPENAI_MAX_TOKENS"
    )
    openai_timeout_sec: int = Field(
        default=90,
        alias="OPENAI_TIMEOUT_SEC"
    )

    llm_max_chars: int = Field(
        default=12000,
        alias="LLM_MAX_CHARS"
    )

    # ======================================
    # Whisper ASR
    # ======================================

    asr_model_size: str = Field(
        default="small",
        alias="ASR_MODEL_SIZE"
    )

    asr_language: str = Field(
        default="pt",
        alias="ASR_LANGUAGE"
    )

    asr_compute_type: str = Field(
        default="int8",
        alias="ASR_COMPUTE_TYPE"
    )

    asr_beam_size: int = Field(
        default=1,
        alias="ASR_BEAM_SIZE"
    )

    asr_vad_filter: bool = Field(
        default=True,
        alias="ASR_VAD_FILTER"
    )

    # ======================================
    # Long Video Transcription
    # ======================================

    asr_segment_duration_sec: int = Field(
        default=600,
        alias="ASR_SEGMENT_DURATION_SEC"
    )

    asr_parallel_workers: int = Field(
        default=2,
        alias="ASR_PARALLEL_WORKERS"
    )

    diarization_enabled: bool = Field(
        default=False,
        alias="DIARIZATION_ENABLED"
    )

    diarization_model_name: str = Field(
        default="pyannote/speaker-diarization-3.1",
        alias="DIARIZATION_MODEL_NAME"
    )

    diarization_hf_token: str | None = Field(
        default=None,
        alias="DIARIZATION_HF_TOKEN"
    )

    diarization_min_overlap_sec: float = Field(
        default=0.15,
        alias="DIARIZATION_MIN_OVERLAP_SEC"
    )

    qa_enabled: bool = Field(
        default=True,
        alias="QA_ENABLED"
    )

    qa_min_clip_duration_sec: int = Field(
        default=25,
        alias="QA_MIN_CLIP_DURATION_SEC"
    )

    qa_max_clip_duration_sec: int = Field(
        default=90,
        alias="QA_MAX_CLIP_DURATION_SEC"
    )

    qa_max_speakers_per_clip: int = Field(
        default=3,
        alias="QA_MAX_SPEAKERS_PER_CLIP"
    )

    render_min_clip_duration_sec: int = Field(
        default=25,
        alias="RENDER_MIN_CLIP_DURATION_SEC"
    )

    # ======================================
    # Telegram
    # ======================================

    telegram_bot_token: str | None = Field(
        default=None,
        alias="TELEGRAM_BOT_TOKEN"
    )

    telegram_chat_id: str | None = Field(
        default=None,
        alias="TELEGRAM_CHAT_ID"
    )
    telegram_disable_notifications: bool = Field(
        default=False,
        alias="TELEGRAM_DISABLE_NOTIFICATIONS"
    )
    telegram_timeout_sec: int = Field(
        default=30,
        alias="TELEGRAM_TIMEOUT_SEC"
    )
    telegram_upload_timeout_sec: int = Field(
        default=300,
        alias="TELEGRAM_UPLOAD_TIMEOUT_SEC"
    )

    # ======================================
    # Integration retries
    # ======================================

    integration_retry_attempts: int = Field(
        default=3,
        alias="INTEGRATION_RETRY_ATTEMPTS"
    )
    integration_retry_min_sec: int = Field(
        default=2,
        alias="INTEGRATION_RETRY_MIN_SEC"
    )
    integration_retry_max_sec: int = Field(
        default=10,
        alias="INTEGRATION_RETRY_MAX_SEC"
    )

    clipflow_api_enabled: bool = Field(
        default=False,
        alias="CLIPFLOW_API_ENABLED"
    )
    clipflow_api_base_url: str | None = Field(
        default=None,
        alias="CLIPFLOW_API_BASE_URL"
    )
    clipflow_api_timeout_sec: int = Field(
        default=15,
        alias="CLIPFLOW_API_TIMEOUT_SEC"
    )

    # ======================================
    # Redis
    # ======================================

    redis_host: str = Field(
        default="redis.voxmind-v2.svc.cluster.local",
        alias="VOXMIND_REDIS_HOST"
    )

    redis_port: int = Field(
        default=6379,
        alias="VOXMIND_REDIS_PORT"
    )

    redis_queue_name: str = Field(
        default="voxmind_jobs",
        alias="VOXMIND_REDIS_QUEUE"
    )


settings = Settings()
