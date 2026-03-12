from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import UserRole, UserStatus


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_phone_status", "phone_number", "status"),
    )

    # ==============================
    # Identidade básica
    # ==============================

    phone_number: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        unique=True,
    )

    full_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # ==============================
    # Autorização / Controle
    # ==============================

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role_enum"),
        nullable=False,
        default=UserRole.CUSTOMER,
    )

    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status_enum"),
        nullable=False,
        default=UserStatus.ACTIVE,
    )

    # ==============================
    # Sistema de créditos
    # ==============================

    credits: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # ==============================
    # Segurança JWT
    # ==============================

    token_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )

    token_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ==============================
    # Fingerprint leve
    # ==============================

    fingerprint_hash: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    last_login_ip: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    marketing_opt_in: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    # ==============================
    # Billing externo
    # ==============================

    stripe_customer_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
    )

    mercadopago_customer_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
    )

    # ==============================
    # OTP / Challenge
    # ==============================

    otp_hash: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    otp_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    otp_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    otp_last_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    otp_locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    otp_challenge_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
    )

    otp_challenge_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ==============================
    # Relacionamentos
    # ==============================

    purchases = relationship(
        "Purchase",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    jobs = relationship(
        "ClipJob",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    idempotency_keys = relationship(
        "IdempotencyKey",
        back_populates="user",
        cascade="all, delete-orphan",
    )