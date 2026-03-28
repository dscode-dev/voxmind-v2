from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )

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
    internal_api_token: str | None = Field(default=None, alias="INTERNAL_API_TOKEN")
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
    fixed_test_otp: str = Field(
        default="123456",
        alias="FIXED_TEST_OTP",
    )

    # =====================================
    # Storage
    # =====================================

    minio_endpoint: str = Field(alias="MINIO_ENDPOINT")
    minio_access_key: str = Field(alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field(alias="MINIO_SECRET_KEY")
    minio_secure: bool = Field(default=False, alias="MINIO_SECURE")
    minio_bucket: str = Field(default="clipflow")
    worker_artifacts_bucket: str = Field(
        default="voxmind",
        alias="WORKER_ARTIFACTS_BUCKET",
    )
    signed_asset_url_expiry_sec: int = Field(
        default=3600,
        alias="SIGNED_ASSET_URL_EXPIRY_SEC",
    )


settings = Settings()
