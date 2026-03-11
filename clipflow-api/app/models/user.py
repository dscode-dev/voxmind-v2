from __future__ import annotations

from sqlalchemy import Boolean, Enum, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import UserRole, UserStatus


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_email_status", "email", "status"),
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role_enum"),
        nullable=False,
        default=UserRole.CUSTOMER,
    )

    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status_enum"),
        nullable=False,
        default=UserStatus.PENDING_VERIFICATION,
    )

    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    marketing_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    mercadopago_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)

    last_login_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    purchases = relationship("Purchase", back_populates="user", cascade="all, delete-orphan")
    jobs = relationship("ClipJob", back_populates="user", cascade="all, delete-orphan")
    idempotency_keys = relationship("IdempotencyKey", back_populates="user", cascade="all, delete-orphan")