from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class OAuthState(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One in-flight OAuth authorization, identified by its ``state`` parameter.

    The ``state`` parameter is the only thing standing between this system and an attacker
    walking a logged-in admin through a callback that binds *the attacker's* YouTube channel
    to the platform. Every property that makes it work is enforced here rather than in a
    cookie or in memory:

    * **Unguessable** — 32 bytes from ``secrets.token_urlsafe``, not a uuid4 chosen for
      convenience.
    * **Single-use** — ``consumed_at`` is set inside the same transaction that reads it, so a
      replayed callback finds it already spent.
    * **Expiring** — a state that was never completed stops being usable, so an authorization
      URL left open in a tab overnight cannot be finished the next day.
    * **Bound to a person** — ``actor_user_id`` records which admin started the flow, so the
      audit trail names them rather than "someone".

    A table rather than a signed cookie or process memory: the callback can land on a
    different replica than the one that issued the URL, and single-use is a claim only shared
    storage can actually enforce.
    """

    __tablename__ = "oauth_states"

    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="youtube")
    state: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # The redirect URI this state was issued against, so the exchange can send back exactly
    # the value the authorization used. Google rejects a mismatch, and reconstructing it at
    # callback time from anything request-derived is how that becomes a spoofable input.
    redirect_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Non-secret bookkeeping only: never the code, never a token.
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
