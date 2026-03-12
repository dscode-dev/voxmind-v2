import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class JobLease(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "job_leases"

    __table_args__ = (
        Index("ix_job_leases_job", "job_id"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clip_jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)