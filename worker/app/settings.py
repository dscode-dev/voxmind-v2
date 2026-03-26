from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ======================================
    # Core Pipeline Configuration
    # ======================================

    video_url: str | None = Field(default=None, alias="VIDEO_URL")

    pipeline_mode: str = Field(default="v2", alias="PIPELINE_MODE")
    worker_mode: str = Field(default="queue", alias="WORKER_MODE")

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
        default=18000,
        alias="LLM_MAX_CHARS"
    )
    prompt_max_candidates: int = Field(
        default=5,
        alias="PROMPT_MAX_CANDIDATES"
    )
    prompt_max_segments_per_candidate: int = Field(
        default=10,
        alias="PROMPT_MAX_SEGMENTS_PER_CANDIDATE"
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
    asr_fallback_to_cpu_on_oom: bool = Field(
        default=True,
        alias="ASR_FALLBACK_TO_CPU_ON_OOM"
    )
    asr_device: str = Field(
        default="cpu",
        alias="ASR_DEVICE"
    )
    asr_cpu_threads: int = Field(
        default=4,
        alias="ASR_CPU_THREADS"
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
    asr_max_merged_segment_duration_sec: int = Field(
        default=18,
        alias="ASR_MAX_MERGED_SEGMENT_DURATION_SEC"
    )

    diarization_enabled: bool = Field(
        default=False,
        alias="DIARIZATION_ENABLED"
    )

    diarization_model_name: str = Field(
        default="pyannote/speaker-diarization-3.1",
        alias="DIARIZATION_MODEL_NAME"
    )
    diarization_device: str = Field(
        default="cpu",
        alias="DIARIZATION_DEVICE"
    )
    diarization_fallback_to_cpu_on_oom: bool = Field(
        default=True,
        alias="DIARIZATION_FALLBACK_TO_CPU_ON_OOM"
    )

    diarization_hf_token: str | None = Field(
        default=None,
        alias="DIARIZATION_HF_TOKEN"
    )

    diarization_min_overlap_sec: float = Field(
        default=0.15,
        alias="DIARIZATION_MIN_OVERLAP_SEC"
    )

    clipsai_enabled: bool = Field(
        default=True,
        alias="CLIPSAI_ENABLED"
    )
    clipsai_device: str = Field(
        default="cuda",
        alias="CLIPSAI_DEVICE"
    )
    clipsai_fallback_to_cpu_on_oom: bool = Field(
        default=True,
        alias="CLIPSAI_FALLBACK_TO_CPU_ON_OOM"
    )
    clipsai_max_candidates: int = Field(
        default=6,
        alias="CLIPSAI_MAX_CANDIDATES"
    )
    clipsai_min_candidate_duration_sec: int = Field(
        default=26,
        alias="CLIPSAI_MIN_CANDIDATE_DURATION_SEC"
    )
    clipsai_max_candidate_duration_sec: int = Field(
        default=80,
        alias="CLIPSAI_MAX_CANDIDATE_DURATION_SEC"
    )

    qa_enabled: bool = Field(
        default=True,
        alias="QA_ENABLED"
    )
    candidate_max_duration_sec: int = Field(
        default=90,
        alias="CANDIDATE_MAX_DURATION_SEC"
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

    auto_review_enabled: bool = Field(
        default=True,
        alias="AUTO_REVIEW_ENABLED"
    )

    auto_review_ready_score_threshold: int = Field(
        default=85,
        alias="AUTO_REVIEW_READY_SCORE_THRESHOLD"
    )

    auto_review_blocked_score_threshold: int = Field(
        default=45,
        alias="AUTO_REVIEW_BLOCKED_SCORE_THRESHOLD"
    )

    auto_review_max_review_clips: int = Field(
        default=1,
        alias="AUTO_REVIEW_MAX_REVIEW_CLIPS"
    )

    render_min_clip_duration_sec: int = Field(
        default=25,
        alias="RENDER_MIN_CLIP_DURATION_SEC"
    )
    render_boundary_snap_tolerance_sec: float = Field(
        default=8.0,
        alias="RENDER_BOUNDARY_SNAP_TOLERANCE_SEC"
    )
    render_sequence_bridge_max_gap_sec: float = Field(
        default=10.0,
        alias="RENDER_SEQUENCE_BRIDGE_MAX_GAP_SEC"
    )
    render_context_backfill_max_sec: float = Field(
        default=2.5,
        alias="RENDER_CONTEXT_BACKFILL_MAX_SEC"
    )
    render_final_closure_extension_max_sec: float = Field(
        default=14.0,
        alias="RENDER_FINAL_CLOSURE_EXTENSION_MAX_SEC"
    )
    short_serie_max_gap_sec: int = Field(
        default=22,
        alias="SHORT_SERIE_MAX_GAP_SEC"
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
    clipflow_api_internal_token: str | None = Field(
        default=None,
        alias="CLIPFLOW_API_INTERNAL_TOKEN"
    )
    scheduler_poll_interval_sec: int = Field(
        default=300,
        alias="SCHEDULER_POLL_INTERVAL_SEC"
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
