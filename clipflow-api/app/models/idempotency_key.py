from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class IdempotencyKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        Index("ix_idempotency_keys_user_key", "user_id", "key", unique=True),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    request_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    response_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    user = relationship("User", back_populates="idempotency_keys")