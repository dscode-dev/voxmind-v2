from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PublishAttemptStatus, PublishRetryability


class PublishAttempt(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One logical publication of one media item to one target.

    **One row per logical publication, not per network attempt.** Retrying does not create a
    second row; it increments ``attempt_no`` on this one. That is what makes the unique index
    on ``idempotency_key`` a real guarantee rather than a hint: there is no legitimate reason
    for two rows to exist for the same job/target/media, so the database can simply forbid it.

    The row is committed *before* any byte is sent. An upload whose response is lost then has
    somewhere to be recorded; an upload with no row would be a video on the internet that this
    system has no memory of.
    """

    __tablename__ = "publish_attempts"
    __table_args__ = (
        Index("ix_publish_attempts_job", "pipeline_job_id"),
        # The identity constraint. Not partial: a superseded attempt is expressed by bumping
        # the version suffix inside the key, exactly as admission does, so "one row per key"
        # holds unconditionally and no state has to be consulted to know whether a duplicate
        # is allowed.
        Index("uq_publish_attempts_idempotency", "idempotency_key", unique=True),
        # Finding the attempts that need a human, without scanning history.
        Index(
            "ix_publish_attempts_unresolved",
            "status",
            postgresql_where=text("status IN ('UNKNOWN', 'NEEDS_MANUAL_RESOLUTION')"),
        ),
    )

    pipeline_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipeline_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("publish_targets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # ---------------------------------------------------------------- identity
    # publish:<pipeline_job_id>:<target_id>:<media_identity>:v1 — deterministic, derived
    # only from what is being published and where. Never a timestamp or a random value:
    # those make every retry a new identity, which is the opposite of what this is for.
    idempotency_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Which media item of the job this is. A PipelineJob can render several final clips and
    # each is its own publication, so the job id alone does not identify what was uploaded.
    media_identity: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The MinIO object actually uploaded, recorded so a published video can be traced back
    # to the exact bytes that produced it.
    media_storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # ------------------------------------------------------------------ state
    status: Mapped[PublishAttemptStatus] = mapped_column(
        Enum(PublishAttemptStatus, name="publish_attempt_status_enum"),
        nullable=False,
        default=PublishAttemptStatus.PENDING,
    )
    retryability: Mapped[PublishRetryability | None] = mapped_column(
        Enum(PublishRetryability, name="publish_retryability_enum"),
        nullable=True,
    )

    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    # ----------------------------------------------------------------- result
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # How the external id came to be here. A video id an operator supplied after
    # investigating an UNKNOWN is not the same evidence as one the API returned, and a
    # future audit should not have to guess which it was looking at.
    external_id_source: Mapped[str | None] = mapped_column(String(32), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # A provider error *code* ("quotaExceeded", "invalid_grant") or an exception class name.
    # Never a response body: the provider echoes request parameters into some of its errors.
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ------------------------------------------------------------- operational
    # The resumable session URI. Treated as a credential: it authorises writes to this
    # upload for anyone holding it, so it is encrypted and never serialised by the API.
    upload_session_uri_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    bytes_uploaded: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # The metadata actually sent, frozen at creation. publish_package.json can be rewritten
    # by a re-render; what went to YouTube cannot. Also holds the request/target/system
    # precedence trace, so "why did it get this title" is answerable from the row.
    payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Non-secret provider response fields (upload status, privacy as accepted, etc).
    provider_metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    job = relationship("PipelineJob", back_populates="publish_attempts")
    target = relationship("PublishTarget", back_populates="attempts")

    @property
    def is_settled(self) -> bool:
        """Whether this publication has an answer that no further upload could change."""
        return self.status in {
            PublishAttemptStatus.SUCCEEDED,
            PublishAttemptStatus.FAILED_FINAL,
            PublishAttemptStatus.CANCELED,
        }

    @property
    def needs_human(self) -> bool:
        return self.status in {
            PublishAttemptStatus.UNKNOWN,
            PublishAttemptStatus.NEEDS_MANUAL_RESOLUTION,
        }
