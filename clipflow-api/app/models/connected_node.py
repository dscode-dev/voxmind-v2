from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ConnectedNodeStatus


class ConnectedNode(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An automation node (e.g. the OpenClaw iOS node on the iPad M4). Backs the
    "connected nodes" ops panel. The node is optional — its absence never blocks the platform."""

    __tablename__ = "connected_nodes"

    node_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, default="openclaw_ios")

    status: Mapped[ConnectedNodeStatus] = mapped_column(
        Enum(ConnectedNodeStatus, name="connected_node_status_enum"),
        nullable=False,
        default=ConnectedNodeStatus.UNKNOWN,
    )

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    capabilities_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
