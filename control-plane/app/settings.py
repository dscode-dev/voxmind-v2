from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # =========================
    # Environment
    # =========================
    env: str = Field(default="prod", description="Environment name")

    # =========================
    # Kubernetes
    # =========================
    namespace: str = Field(default="voxmind-v2", alias="VOXMIND_NAMESPACE")
    worker_job_image: str = Field(
        default="192.168.122.50:30500/voxmind-worker:local",
        alias="WORKER_JOB_IMAGE",
    )

    worker_job_ttl_seconds_after_finished: int = Field(
        default=3600, alias="WORKER_JOB_TTL"
    )

    worker_job_active_deadline_seconds: int = Field(
        default=21600, alias="WORKER_JOB_DEADLINE"
    )

    worker_cpu_request: str = Field(default="1000m", alias="WORKER_CPU_REQUEST")
    worker_cpu_limit: str = Field(default="2000m", alias="WORKER_CPU_LIMIT")
    worker_mem_request: str = Field(default="2Gi", alias="WORKER_MEM_REQUEST")
    worker_mem_limit: str = Field(default="4Gi", alias="WORKER_MEM_LIMIT")
    worker_gpu_resource_key: str = Field(
        default="nvidia.com/gpu",
        alias="WORKER_GPU_RESOURCE_KEY",
    )
    worker_gpu_request: str = Field(default="1", alias="WORKER_GPU_REQUEST")
    worker_gpu_limit: str = Field(default="1", alias="WORKER_GPU_LIMIT")
    worker_cuda_visible_devices: str = Field(
        default="all",
        alias="WORKER_CUDA_VISIBLE_DEVICES",
    )

    # =========================
    # MinIO (OBRIGATÓRIO)
    # =========================
    minio_endpoint: str = Field(
        default="minio.voxmind-v2.svc.cluster.local:9000", alias="MINIO_ENDPOINT"
    )

    minio_bucket: str = Field(default="voxmind-artifacts", alias="MINIO_BUCKET")

    minio_root_user: str = Field(..., alias="MINIO_ROOT_USER")
    minio_root_password: str = Field(..., alias="MINIO_ROOT_PASSWORD")

    # =========================
    # Observability
    # =========================
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    health_host: str = Field(default="0.0.0.0", alias="HEALTH_HOST")
    health_port: int = Field(default=8000, alias="HEALTH_PORT")

    # =========================
    # Telegram (Control Plane)
    # =========================
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(..., alias="TELEGRAM_CHAT_ID")

    # =========================
    # Redis Queue
    # =========================
    redis_host: str = Field(
        default="redis.voxmind-v2.svc.cluster.local", alias="VOXMIND_REDIS_HOST"
    )

    redis_port: int = Field(default=6379, alias="VOXMIND_REDIS_PORT")

    redis_queue_name: str = Field(default="voxmind_jobs", alias="VOXMIND_REDIS_QUEUE")
    redis_job_registry_prefix: str = Field(
        default="voxmind:job_registry",
        alias="VOXMIND_JOB_REGISTRY_PREFIX",
    )
    job_registry_ttl_sec: int = Field(
        default=604800,
        alias="VOXMIND_JOB_REGISTRY_TTL_SEC",
    )
    clipflow_api_base_url: str | None = Field(
        default=None,
        alias="CLIPFLOW_API_BASE_URL",
    )
    clipflow_api_internal_token: str | None = Field(
        default=None,
        alias="CLIPFLOW_API_INTERNAL_TOKEN",
    )

    # =========================
    # Bot validation
    # =========================
    min_cut_duration_sec: int = Field(
        default=25,
        alias="MIN_CUT_DURATION_SEC",
    )
    min_final_video_duration_sec: int = Field(
        default=60,
        alias="MIN_FINAL_VIDEO_DURATION_SEC",
    )
    min_internal_cut_duration_sec: int = Field(
        default=12,
        alias="MIN_INTERNAL_CUT_DURATION_SEC",
    )
    max_final_video_duration_sec: int = Field(
        default=120,
        alias="MAX_FINAL_VIDEO_DURATION_SEC",
    )
    short_serie_max_gap_sec: int = Field(
        default=22,
        alias="SHORT_SERIE_MAX_GAP_SEC",
    )


settings = Settings()
