from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, model_validator


# Secrets that were previously accepted as silent fallbacks. Refusing them here turns a
# misconfiguration into a startup failure instead of an authentication bypass.
FORBIDDEN_SECRET_VALUES = {
    "",
    "clipflow-secret",
    "change_me_jwt_secret",
    "change_me_internal_token",
}

DEVELOPMENT_ENVIRONMENTS = {"development", "dev", "test", "testing", "local"}


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )

    # =====================================
    # Environment
    # =====================================

    environment: str = Field(default="production", alias="ENVIRONMENT")

    @property
    def is_development(self) -> bool:
        """True only for explicitly non-production environments.

        Anything unrecognised — including an unset ENVIRONMENT — is treated as production,
        so a missing value can never unlock a development-only affordance.
        """
        return str(self.environment or "").strip().lower() in DEVELOPMENT_ENVIRONMENTS

    # =====================================
    # Database
    # =====================================

    database_url: str = Field(
        default="postgresql+psycopg://clipflow:clipflow@localhost:5432/clipflow",
        alias="DATABASE_URL"
    )

    database_pool_size: int = Field(default=10, alias="DATABASE_POOL_SIZE")
    database_max_overflow: int = Field(default=20, alias="DATABASE_MAX_OVERFLOW")

    # =====================================
    # API
    # =====================================

    api_name: str = Field(default="ClipFlow API")
    api_version: str = Field(default="1.0.0")

    # =====================================
    # Security
    # =====================================

    jwt_secret: str = Field(alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256")
    jwt_expiration_minutes: int = Field(default=1440)
    cookie_secure: bool = Field(default=True, alias="COOKIE_SECURE")
    cookie_samesite: str = Field(default="lax", alias="COOKIE_SAMESITE")
    cors_allowed_origins: str = Field(
        default="https://sanninjiraiya.lab,http://sanninjiraiya.lab",
        alias="CORS_ALLOWED_ORIGINS",
    )
    # Required. Every /internal/* endpoint is authenticated with this token; without it the
    # worker and control-plane cannot talk to the API, so a missing value is a hard failure
    # rather than an open door.
    internal_api_token: str = Field(alias="INTERNAL_API_TOKEN")
    default_admin_phone_number: str = Field(
        default="+5581999912985",
        alias="DEFAULT_ADMIN_PHONE_NUMBER",
    )
    default_admin_full_name: str = Field(
        default="ClipFlow Admin",
        alias="DEFAULT_ADMIN_FULL_NAME",
    )
    default_admin_credits: int = Field(
        default=999999,
        alias="DEFAULT_ADMIN_CREDITS",
    )
    lab_unlimited_credit_phone_numbers: str = Field(
        default="",
        alias="LAB_UNLIMITED_CREDIT_PHONE_NUMBERS",
    )
    otp_request_limit_per_ip_window: int = Field(
        default=5,
        alias="OTP_REQUEST_LIMIT_PER_IP_WINDOW",
    )
    otp_request_limit_per_phone_window: int = Field(
        default=3,
        alias="OTP_REQUEST_LIMIT_PER_PHONE_WINDOW",
    )
    otp_verify_fail_limit_per_ip_window: int = Field(
        default=10,
        alias="OTP_VERIFY_FAIL_LIMIT_PER_IP_WINDOW",
    )
    otp_rate_limit_window_sec: int = Field(
        default=600,
        alias="OTP_RATE_LIMIT_WINDOW_SEC",
    )
    # Development affordance only. There is no default: an unset value must never produce a
    # guessable code. Honoured exclusively when ENVIRONMENT is a development environment.
    fixed_test_otp: str | None = Field(
        default=None,
        alias="FIXED_TEST_OTP",
    )

    def resolve_fixed_otp(self) -> str | None:
        """Return the configured fixed OTP, or None when one must not be used.

        Requires both an explicit development ENVIRONMENT and an explicit FIXED_TEST_OTP.
        """
        if not self.is_development:
            return None
        code = str(self.fixed_test_otp or "").strip()
        return code or None
    internal_default_product_name: str = Field(
        default="Internal Default",
        alias="INTERNAL_DEFAULT_PRODUCT_NAME",
    )
    internal_default_product_description: str = Field(
        default="Produto técnico padrão para operação interna do ClipFlow",
        alias="INTERNAL_DEFAULT_PRODUCT_DESCRIPTION",
    )
    internal_default_product_max_video_duration_sec: int = Field(
        default=14400,
        alias="INTERNAL_DEFAULT_PRODUCT_MAX_VIDEO_DURATION_SEC",
    )
    internal_default_product_max_shorts_generated: int = Field(
        default=10,
        alias="INTERNAL_DEFAULT_PRODUCT_MAX_SHORTS_GENERATED",
    )

    # =====================================
    # Storage
    # =====================================

    # Internal endpoint — reachable only inside the Compose network. Used for every
    # service-to-service call (stat, get, put).
    minio_endpoint: str = Field(alias="MINIO_ENDPOINT")
    minio_access_key: str = Field(alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field(alias="MINIO_SECRET_KEY")
    minio_secure: bool = Field(default=False, alias="MINIO_SECURE")

    # Public endpoint — the host:port a browser can actually reach. Presigned URLs must be
    # signed against this host, because SigV4 covers the Host header: rewriting the host
    # after signing invalidates the signature. Falls back to the internal endpoint when
    # unset, which reproduces the previous (browser-unreachable) behaviour.
    minio_public_endpoint: str | None = Field(default=None, alias="MINIO_PUBLIC_ENDPOINT")
    minio_public_secure: bool | None = Field(default=None, alias="MINIO_PUBLIC_SECURE")

    @property
    def resolved_minio_public_endpoint(self) -> str:
        return (self.minio_public_endpoint or "").strip() or self.minio_endpoint

    @property
    def resolved_minio_public_secure(self) -> bool:
        if self.minio_public_secure is None:
            return self.minio_secure
        return self.minio_public_secure

    minio_bucket: str = Field(default="clipflow")
    worker_artifacts_bucket: str = Field(
        default="voxmind",
        alias="WORKER_ARTIFACTS_BUCKET",
    )
    signed_asset_url_expiry_sec: int = Field(
        default=3600,
        alias="SIGNED_ASSET_URL_EXPIRY_SEC",
    )

    # =====================================
    # Worker Queue
    # =====================================

    voxmind_redis_host: str = Field(
        default="redis.voxmind-v2.svc.cluster.local",
        alias="VOXMIND_REDIS_HOST",
    )
    voxmind_redis_port: int = Field(
        default=6379,
        alias="VOXMIND_REDIS_PORT",
    )
    voxmind_redis_queue: str = Field(
        default="voxmind_jobs",
        alias="VOXMIND_REDIS_QUEUE",
    )

    # =====================================
    # Script Agent
    # =====================================

    script_agent_provider: str = Field(
        default="local",
        alias="SCRIPT_AGENT_PROVIDER",
    )
    script_agent_openai_api_key: str | None = Field(
        default=None,
        alias="SCRIPT_AGENT_OPENAI_API_KEY",
    )
    script_agent_openai_model: str = Field(
        default="gpt-4o-mini",
        alias="SCRIPT_AGENT_OPENAI_MODEL",
    )
    script_agent_timeout_sec: int = Field(
        default=45,
        alias="SCRIPT_AGENT_TIMEOUT_SEC",
    )

    # =====================================
    # Discovery (PR-DISCOVERY-01)
    # -------------------------------------------------------------------------------
    # Optional. With no key the YouTube provider reports itself unavailable and the API
    # still boots — an optional integration must not be able to stop the stack, and a
    # missing credential must never be replaced by fabricated results.
    # =====================================

    youtube_api_key: str | None = Field(default=None, alias="YOUTUBE_API_KEY")
    discovery_http_timeout_sec: float = Field(
        default=15.0, alias="DISCOVERY_HTTP_TIMEOUT_SEC"
    )
    # Per query. YouTube's own ceiling is 50; a search costs 100 quota units of a 10,000
    # daily allowance, so this is the main lever on how much of it one run spends.
    discovery_max_results: int = Field(default=25, alias="DISCOVERY_MAX_RESULTS")
    # How far back a run looks when the topic does not say.
    discovery_freshness_days: int = Field(default=7, alias="DISCOVERY_FRESHNESS_DAYS")

    # =====================================
    # Selection (PR-SELECTION-01)
    # -------------------------------------------------------------------------------
    # Optional. With no key the semantic evaluator reports itself unavailable and selection
    # continues on deterministic signals alone — with a raised score threshold, because less
    # evidence should mean more caution, not a fabricated relevance number.
    #
    # Policy (weights, caps, thresholds) is NOT here: it belongs to the editorial intention
    # and lives in ContentTopic.metadata_json["selection"].
    # =====================================

    selection_openai_api_key: str | None = Field(
        default=None, alias="SELECTION_OPENAI_API_KEY"
    )
    selection_model: str = Field(default="gpt-4o-mini", alias="SELECTION_MODEL")
    selection_timeout_sec: float = Field(default=20.0, alias="SELECTION_TIMEOUT_SEC")

    # =====================================
    # Validation
    # =====================================

    @field_validator("jwt_secret", "internal_api_token")
    @classmethod
    def _reject_placeholder_secrets(cls, value: str, info) -> str:
        candidate = str(value or "").strip()
        if candidate.lower() in FORBIDDEN_SECRET_VALUES:
            raise ValueError(
                f"{info.field_name} is unset or still set to a known placeholder. "
                "Set a unique, non-default value before starting the API."
            )
        if len(candidate) < 16:
            raise ValueError(
                f"{info.field_name} must be at least 16 characters."
            )
        return candidate

    @model_validator(mode="after")
    def _reject_fixed_otp_outside_development(self) -> "Settings":
        if self.fixed_test_otp and not self.is_development:
            raise ValueError(
                "FIXED_TEST_OTP is set but ENVIRONMENT is not a development environment. "
                "A fixed OTP must never be reachable in production — unset FIXED_TEST_OTP "
                f"or set ENVIRONMENT to one of: {sorted(DEVELOPMENT_ENVIRONMENTS)}."
            )
        return self


settings = Settings()
