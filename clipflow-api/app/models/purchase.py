from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import BillingProvider, PurchaseStatus


class Purchase(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "purchases"
    __table_args__ = (
        Index("ix_purchases_user_status", "user_id", "status"),
        Index("ix_purchases_provider_ext_id", "billing_provider", "provider_payment_id"),
        CheckConstraint("amount_total > 0", name="purchases_amount_positive"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_products.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    billing_provider: Mapped[BillingProvider] = mapped_column(
        Enum(BillingProvider, name="billing_provider_enum"),
        nullable=False,
    )

    status: Mapped[PurchaseStatus] = mapped_column(
        Enum(PurchaseStatus, name="purchase_status_enum"),
        nullable=False,
        default=PurchaseStatus.PENDING,
    )

    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    amount_total: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    provider_payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_checkout_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    user = relationship("User", back_populates="purchases")
    product = relationship("BillingProduct", back_populates="purchases")
    jobs = relationship("ClipJob", back_populates="purchase")