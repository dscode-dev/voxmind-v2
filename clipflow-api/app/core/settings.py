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

    # =====================================
    # Storage
    # =====================================

    minio_endpoint: str = Field(alias="MINIO_ENDPOINT")
    minio_access_key: str = Field(alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field(alias="MINIO_SECRET_KEY")
    minio_bucket: str = Field(default="clipflow")


settings = Settings()