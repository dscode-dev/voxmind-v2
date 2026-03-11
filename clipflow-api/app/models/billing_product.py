from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Enum, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ProductType


class BillingProduct(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "billing_products"
    __table_args__ = (
        CheckConstraint("max_video_duration_sec > 0", name="billing_products_max_video_duration_positive"),
        CheckConstraint("price_amount > 0", name="billing_products_price_positive"),
    )

    code: Mapped[ProductType] = mapped_column(
        Enum(ProductType, name="product_type_enum"),
        nullable=False,
        unique=True,
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    price_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    max_video_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False)

    max_shorts_generated: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    purchases = relationship("Purchase", back_populates="product")
    jobs = relationship("ClipJob", back_populates="product")