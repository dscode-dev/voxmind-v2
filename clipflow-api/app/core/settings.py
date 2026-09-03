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
    # Autonomous pipeline (PR-SCHEDULER-01)
    # -------------------------------------------------------------------------------
    # The global kill switch. Default OFF: this is the flag that lets the system start
    # production on its own, and a deployment that has not deliberately enabled it should
    # not begin doing so because a new image shipped.
    #
    # Turning it off stops the WORK, not the scheduler — the loop keeps ticking and the API
    # stays healthy, so re-enabling needs no restart.
    #
    # Per-topic policy (interval, stage switches, limits) is NOT here: it belongs to the
    # editorial intention and lives in ContentTopic.metadata_json["automation"].
    # =====================================

    autonomous_pipeline_enabled: bool = Field(
        default=False, alias="AUTONOMOUS_PIPELINE_ENABLED"
    )
    # How often the loop looks for due topics. Not the run interval — that is per topic and
    # persisted, so this only bounds how late a due topic can be noticed.
    automation_poll_interval_sec: int = Field(
        default=60, alias="AUTOMATION_POLL_INTERVAL_SEC"
    )
    # A pause before the first tick so a restart does not fire mid-bootstrap.
    automation_startup_delay_sec: int = Field(
        default=15, alias="AUTOMATION_STARTUP_DELAY_SEC"
    )
    # Lets the in-process loop be turned off entirely (to run it elsewhere) without changing
    # the kill switch, which governs whether automation may act at all.
    automation_runner_enabled: bool = Field(
        default=True, alias="AUTOMATION_RUNNER_ENABLED"
    )

    # Application logs are dropped without a handler on the root logger: uvicorn configures
    # only its own loggers, so everything the application reports would go nowhere. INFO is
    # the level at which a tick that did something is visible and an idle one is not.
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # =====================================
    # Publishing (PR-PUBLISH-01)
    # -------------------------------------------------------------------------------
    # The global publish kill switch. Default OFF, and for a harder reason than the
    # automation one: publishing is the only boundary in this system that is irreversible
    # from the inside. A duplicated render costs GPU time; a duplicated upload is public.
    #
    # Nothing calls the publisher automatically in this PR — the switch guards the manual
    # command, so an operator cannot publish from a deployment that never opted in.
    # =====================================

    publishing_enabled: bool = Field(default=False, alias="PUBLISHING_ENABLED")

    # OAuth client credentials for the YouTube Data API. Absent means "provider not
    # configured" — never a fake token and never a silent no-op upload.
    youtube_client_id: str | None = Field(default=None, alias="YOUTUBE_CLIENT_ID")
    youtube_client_secret: str | None = Field(default=None, alias="YOUTUBE_CLIENT_SECRET")
    # From configuration, never from a request header: deriving it from Host or
    # X-Forwarded-* would let a spoofed header redirect an authorization code elsewhere.
    youtube_oauth_redirect_uri: str | None = Field(
        default=None, alias="YOUTUBE_OAUTH_REDIRECT_URI"
    )

    # Encrypts refresh tokens at rest. Urlsafe-base64, 32 bytes (Fernet). Without it a
    # target cannot be connected at all — see app/security/secret_box.py.
    publish_secret_key: str | None = Field(default=None, alias="PUBLISH_SECRET_KEY")

    # How long a run may spend uploading before the client gives up. A timeout here is
    # exactly the ambiguous case: the bytes may have landed, so it resolves to UNKNOWN and
    # never to a blind retry.
    youtube_upload_timeout_sec: float = Field(
        default=900.0, alias="YOUTUBE_UPLOAD_TIMEOUT_SEC"
    )
    # Resumable upload chunk size, in MiB. 8 is a multiple of the 256 KiB the API requires
    # and small enough that a broken connection loses little work.
    youtube_upload_chunk_mib: int = Field(default=8, alias="YOUTUBE_UPLOAD_CHUNK_MIB")

    # =====================================
    # Publication runtime (PR-PUBLISH-QUEUE-01)
    # -------------------------------------------------------------------------------
    # A queue of its own, never the media queue. A render retry costs GPU time; a publication
    # retry may cost a duplicate public video, so the two need different visibility timeouts,
    # different budgets, and different rules about when a redelivery may repeat the work.
    # =====================================

    publish_queue_name: str = Field(
        default="clipflow_publish_jobs", alias="PUBLISH_QUEUE_NAME"
    )
    # Runs the publication loop in this process. Separate from PUBLISHING_ENABLED, which says
    # whether publishing may happen at all: this says whether THIS process is the one doing
    # it. The API sets it false and the publisher container sets it true.
    publisher_runtime_enabled: bool = Field(
        default=False, alias="PUBLISHER_RUNTIME_ENABLED"
    )
    # How long a claimed command stays invisible without a heartbeat. Generous, because an
    # upload legitimately takes minutes and recovering a command that is still uploading is
    # the expensive mistake.
    publish_visibility_timeout_sec: int = Field(
        default=900, alias="PUBLISH_VISIBILITY_TIMEOUT_SEC"
    )
    # Well under the visibility timeout, so a live worker renews several times before it
    # could ever be considered dead.
    publish_heartbeat_interval_sec: int = Field(
        default=20, alias="PUBLISH_HEARTBEAT_INTERVAL_SEC"
    )
    publish_heartbeat_ttl_sec: int = Field(default=60, alias="PUBLISH_HEARTBEAT_TTL_SEC")
    publish_claim_block_sec: int = Field(default=5, alias="PUBLISH_CLAIM_BLOCK_SEC")

    # The queue-level execution budget. Small: a publication that has failed this many times
    # needs a person, not another attempt. Multiplied by nothing - the provider client does
    # not retry internally, so this IS the total budget.
    publish_max_attempts: int = Field(default=3, alias="PUBLISH_MAX_ATTEMPTS")
    publish_retry_backoff_base_sec: float = Field(
        default=30.0, alias="PUBLISH_RETRY_BACKOFF_BASE_SEC"
    )
    publish_retry_backoff_max_sec: float = Field(
        default=900.0, alias="PUBLISH_RETRY_BACKOFF_MAX_SEC"
    )
    # Quota is not a transient error. YouTube's daily quota resets on a schedule we are not
    # told, so retrying in 30 seconds burns log space and nothing else - it comes back in an
    # hour by default.
    publish_quota_backoff_sec: float = Field(
        default=3600.0, alias="PUBLISH_QUOTA_BACKOFF_SEC"
    )
    # How often the runtime looks for attempts that were committed but never enqueued, and
    # for commands whose worker died.
    publish_sweep_interval_sec: int = Field(default=60, alias="PUBLISH_SWEEP_INTERVAL_SEC")
    # How long to let an in-flight upload finish after SIGTERM before giving up on it.
    publish_shutdown_grace_sec: float = Field(
        default=30.0, alias="PUBLISH_SHUTDOWN_GRACE_SEC"
    )

    # =====================================
    # Autonomous publication (PR-PUBLISH-02)
    # -------------------------------------------------------------------------------
    # A switch of its own, separate from PUBLISHING_ENABLED, and this separation is the
    # point: a deployment that has deliberately turned on *manual* publishing must not start
    # publishing by itself because a new image shipped. Enabling automation is a second,
    # explicit decision.
    #
    # Every one of these is a ceiling, not a target. The policy is fail-closed: any gate that
    # cannot be evaluated is a gate that did not pass.
    # =====================================

    autopublish_enabled: bool = Field(default=False, alias="AUTOPUBLISH_ENABLED")

    # A third switch, because "publish automatically" and "publish automatically to the whole
    # internet" are different risks. Automatic + private is a rollout stage that can be
    # validated for days before anything becomes visible; collapsing the two would remove
    # that stage entirely.
    autopublish_public_enabled: bool = Field(
        default=False, alias="AUTOPUBLISH_PUBLIC_ENABLED"
    )

    # What automatic publications are published as when nothing more specific is configured.
    # `private` because the safe value is the one you get by not thinking about it.
    autopublish_default_privacy: str = Field(
        default="private", alias="AUTOPUBLISH_DEFAULT_PRIVACY"
    )

    # Deliberately tiny. One per tick means a mistake surfaces after one video rather than
    # after a channel full of them, and the loop runs often enough that a real backlog still
    # drains steadily.
    autopublish_max_per_tick: int = Field(default=1, alias="AUTOPUBLISH_MAX_PER_TICK")
    autopublish_max_per_day: int = Field(default=3, alias="AUTOPUBLISH_MAX_PER_DAY")

    # Operational backpressure, not domain policy: if publications are already piling up
    # unexecuted, creating more only makes the pile bigger.
    autopublish_max_queue_backlog: int = Field(
        default=20, alias="AUTOPUBLISH_MAX_QUEUE_BACKLOG"
    )
    # Not zero-tolerance: one ancient dead-lettered command must not be able to stop
    # publishing forever. It takes a real pile to pause automation.
    autopublish_max_dead_letter: int = Field(
        default=10, alias="AUTOPUBLISH_MAX_DEAD_LETTER"
    )

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


# Server-side ceilings for the autopublish caps. Configuration is an operator's intent, not
# a licence: AUTOPUBLISH_MAX_PER_DAY=100000 is a typo, and the clamp is what makes it a small
# number instead of a channel full of videos.
AUTOPUBLISH_CEILING_PER_TICK = 10
AUTOPUBLISH_CEILING_PER_DAY = 50
AUTOPUBLISH_CEILING_QUEUE_BACKLOG = 500

VALID_AUTOPUBLISH_PRIVACY = ("private", "unlisted", "public")


settings = Settings()
