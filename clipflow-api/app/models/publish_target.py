from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PublishPlatform, PublishTargetConnectionStatus


class PublishTarget(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A destination the factory may publish to, and the credential that reaches it.

    Contract-only until PR-PUBLISH-01; this is the PR that gives it a writer, an OAuth
    credential and a meaning for ``is_active``.

    **Two independent switches, not one.** ``is_active`` is the operator's intent ("may this
    target be published to?"); ``connection_status`` is the credential's condition ("can it
    still be reached?"). Collapsing them would mean a revoked token silently reads as an
    operator having disabled the channel, and reconnecting would look like a policy change.
    """

    __tablename__ = "publish_targets"
    __table_args__ = (
        # One connected target per channel per platform. Without it, running the connect
        # flow twice produces two targets for the same channel, and "publish to the channel"
        # stops having a single answer.
        Index(
            "uq_publish_targets_channel",
            "platform",
            "channel_id",
            unique=True,
            postgresql_where="channel_id IS NOT NULL",
        ),
    )

    platform: Mapped[PublishPlatform] = mapped_column(
        Enum(PublishPlatform, name="publish_platform_enum"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The operator's switch. The connect flow sets it to False explicitly, whatever this
    # column default says: authorizing at Google proves the credential works, not that this
    # is the channel the operator meant to publish to. Enabling is a second, deliberate act.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ---------------------------------------------------------------- identity
    # Resolved from the provider after the token exchange, never from operator input: the
    # authorizing Google account decides which channel the token reaches, and an operator
    # who believes they connected channel A while holding a token for channel B would find
    # out by publishing to the wrong audience.
    channel_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    channel_title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ------------------------------------------------------------- credential
    connection_status: Mapped[PublishTargetConnectionStatus] = mapped_column(
        Enum(PublishTargetConnectionStatus, name="publish_target_connection_status_enum"),
        nullable=False,
        default=PublishTargetConnectionStatus.DISCONNECTED,
    )

    # Fernet ciphertext, written and read only through SecretBox. A dedicated column rather
    # than a key inside config_json so that "the secret" is a named thing the code can
    # refuse to serialise, instead of one entry in a blob that endpoints return wholesale.
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The scopes actually granted, which can be narrower than the ones requested if the user
    # unticked a box on the consent screen. Recorded so a later 403 can be explained.
    granted_scopes: Mapped[str | None] = mapped_column(Text, nullable=True)

    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Why the credential stopped working, as a provider error code ("invalid_grant"), never
    # a message body.
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ------------------------------------------------------------- autopublish
    # A third switch, and not a duplicate of ``is_active``. Active means "this target may be
    # published to"; this means "this target may be published to *without a human deciding
    # each time*". A channel can reasonably allow the first and not the second - that is the
    # whole shape of a careful rollout - so collapsing them would remove the safe middle.
    autopublish_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # When automation was switched on for this target. The cutoff that stops a backlog
    # surprise: enabling autopublish on a channel with fifty finished runs waiting must not
    # publish fifty videos. Only runs that became ready *after* this moment are automatic;
    # the older ones stay publishable by hand, which is where that decision belongs.
    autopublish_enabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---------------------------------------------------------------- defaults
    # Publishing defaults for this target. Non-secret by construction: everything secret has
    # its own column above, so this may be returned by the API in full.
    config_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    attempts = relationship("PublishAttempt", back_populates="target")

    @property
    def is_publishable(self) -> bool:
        """Both switches, in one place, so no caller can check only one of them."""
        return (
            self.is_active
            and self.connection_status == PublishTargetConnectionStatus.CONNECTED
            and bool(self.refresh_token_encrypted)
        )

    @property
    def is_autopublishable(self) -> bool:
        """Everything ``is_publishable`` requires, plus consent to act without a human."""
        return self.is_publishable and self.autopublish_enabled
