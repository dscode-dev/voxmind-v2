"""Connecting a YouTube channel, and keeping the credential honest afterwards.

Everything that touches a refresh token lives here, so there is exactly one place to audit
for "does a secret escape". The rule the whole module is built around: a token enters through
:meth:`complete_connect` and leaves only through :meth:`credential_for`, which is called by
the publishing service and by nothing else.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.enums import PublishPlatform, PublishTargetConnectionStatus
from app.models.oauth_state import OAuthState
from app.models.publish_target import PublishTarget
from app.models.user import User
from app.publishing.contracts import PublishCredential, ProviderNotConfiguredError
from app.publishing.youtube_oauth import (
    UNRECOVERABLE_GRANT_ERRORS,
    OAuthError,
    YouTubeOAuthClient,
)
from app.security.secret_box import SecretBox, SecretDecryptionError, secret_box

logger = logging.getLogger(__name__)


class ConnectError(RuntimeError):
    """The connect flow cannot continue. Message is operator-facing and secret-free."""


class TargetNotPublishableError(RuntimeError):
    """The target exists but may not be published to right now."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PublishTargetService:
    def __init__(
        self,
        oauth: YouTubeOAuthClient | None = None,
        box: SecretBox | None = None,
    ) -> None:
        self.oauth = oauth or YouTubeOAuthClient(
            client_id=settings.youtube_client_id,
            client_secret=settings.youtube_client_secret,
            redirect_uri=settings.youtube_oauth_redirect_uri,
        )
        self.box = box or secret_box

    # ------------------------------------------------------------------- connect

    def begin_connect(self, db: Session, *, actor: User | None) -> dict[str, Any]:
        """Issue an authorization URL and the single-use state that guards its callback."""
        self.oauth.require_configured()
        if not self.box.available:
            # Checked *before* sending the operator to Google. Discovering the key is missing
            # after consent means the authorization code is already spent and the whole
            # consent screen has to be repeated.
            raise ConnectError(
                "PUBLISH_SECRET_KEY is not configured; a refresh token could not be stored "
                "safely, so the connect flow will not start"
            )

        now = datetime.now(timezone.utc)
        state_value = self.oauth.new_state()
        db.add(
            OAuthState(
                provider="youtube",
                state=state_value,
                actor_user_id=actor.id if actor else None,
                expires_at=self.oauth.state_expiry(now),
                redirect_uri=self.oauth.redirect_uri,
            )
        )
        db.flush()
        return {
            "authorization_url": self.oauth.authorization_url(state=state_value),
            "expires_at": self.oauth.state_expiry(now).isoformat(),
        }

    def consume_state(self, db: Session, state_value: str) -> OAuthState:
        """Spend the state exactly once, or refuse.

        Unguessable, unexpired and unspent are all checked here rather than by the caller,
        because an endpoint that forgets one of them is a channel-takeover bug.
        """
        if not state_value:
            raise ConnectError("missing state")

        record = (
            db.query(OAuthState)
            .filter(OAuthState.state == state_value, OAuthState.provider == "youtube")
            .with_for_update()
            .first()
        )
        if record is None:
            raise ConnectError("unknown or already-cleared authorization state")
        if record.consumed_at is not None:
            raise ConnectError("authorization state has already been used")

        expires_at = _as_utc(record.expires_at)
        if expires_at is None or expires_at < datetime.now(timezone.utc):
            raise ConnectError("authorization state has expired; start the connect flow again")

        # Marked spent in the same transaction that read it, so a replay of the same callback
        # finds it consumed rather than racing.
        record.consumed_at = datetime.now(timezone.utc)
        db.flush()
        return record

    def complete_connect(self, db: Session, *, code: str, state_value: str) -> PublishTarget:
        """Exchange the code, resolve the channel, and store the credential encrypted."""
        state = self.consume_state(db, state_value)

        try:
            bundle = self.oauth.exchange_code(code)
        except OAuthError as exc:
            raise ConnectError(f"authorization code exchange failed: {exc.code}") from exc

        # Identity comes from the provider, never from the operator: whoever authorised
        # decides which channel this token reaches.
        try:
            identity = self.oauth.resolve_channel(bundle.access_token)
        except OAuthError as exc:
            raise ConnectError(f"could not resolve the authorized channel: {exc.code}") from exc
        if identity is None:
            raise ConnectError(
                "the authorized Google account has no YouTube channel; create one and "
                "connect again"
            )

        assert bundle.refresh_token is not None  # exchange_code refuses without one
        encrypted = self.box.encrypt(bundle.refresh_token)

        target = (
            db.query(PublishTarget)
            .filter(
                PublishTarget.platform == PublishPlatform.YOUTUBE,
                PublishTarget.channel_id == identity.channel_id,
            )
            .first()
        )
        now = datetime.now(timezone.utc)

        if target is None:
            target = PublishTarget(
                platform=PublishPlatform.YOUTUBE,
                name=identity.channel_title or identity.channel_id,
                channel_id=identity.channel_id,
                # Off until an operator says otherwise. Consent proves the token works, not
                # that this is the channel they meant.
                is_active=False,
                config_json={},
            )
            db.add(target)

        target.channel_title = identity.channel_title
        target.account_ref = identity.channel_id
        target.refresh_token_encrypted = encrypted
        target.granted_scopes = bundle.granted_scopes
        target.connection_status = PublishTargetConnectionStatus.CONNECTED
        target.connected_at = now
        target.last_error_code = None
        db.flush()

        logger.info(
            "publish_target_connected",
            extra={
                "publish_target_id": str(target.id),
                "provider": "youtube",
                "channel_id": identity.channel_id,
                "actor_user_id": str(state.actor_user_id) if state.actor_user_id else None,
            },
        )
        return target

    # ---------------------------------------------------------------- credential

    def credential_for(self, target: PublishTarget) -> PublishCredential:
        """Decrypt the stored refresh token for one upload. The only read path.

        Returns a value object rather than the raw string so the token is never a loose
        variable in the calling code.
        """
        if not target.refresh_token_encrypted:
            raise TargetNotPublishableError("target has no stored credential")
        self.oauth.require_configured()
        try:
            refresh_token = self.box.decrypt(target.refresh_token_encrypted)
        except SecretDecryptionError as exc:
            raise TargetNotPublishableError(
                "stored credential could not be decrypted; reconnect the target"
            ) from exc

        return PublishCredential(
            refresh_token=refresh_token,
            client_id=settings.youtube_client_id or "",
            client_secret=settings.youtube_client_secret or "",
        )

    def mark_reconnect_required(
        self, db: Session, target: PublishTarget, *, error_code: str
    ) -> None:
        """Stop using a credential the provider has rejected.

        Without this, every subsequent publish retries a token that will never work, burning
        quota and filling logs with a failure whose only fix is a human at a consent screen.
        The token ciphertext is dropped: it is now worthless and holding a dead secret is
        strictly worse than holding none.
        """
        target.connection_status = PublishTargetConnectionStatus.RECONNECT_REQUIRED
        target.last_error_code = error_code
        target.refresh_token_encrypted = None
        db.flush()
        logger.warning(
            "publish_target_reconnect_required",
            extra={"publish_target_id": str(target.id), "error_code": error_code},
        )

    @staticmethod
    def is_credential_error(error_code: str | None) -> bool:
        return bool(error_code) and error_code in UNRECOVERABLE_GRANT_ERRORS

    # --------------------------------------------------------------- operations

    def set_enabled(self, db: Session, target: PublishTarget, *, enabled: bool) -> PublishTarget:
        target.is_active = bool(enabled)
        db.flush()
        return target

    def disconnect(self, db: Session, target: PublishTarget, *, revoke: bool = True) -> PublishTarget:
        """Detach the credential. History is kept; the ability to publish is not.

        Local removal happens whatever the provider says: a Google outage must not be able to
        keep a channel attached after an operator has said to detach it.
        """
        token: str | None = None
        if revoke and target.refresh_token_encrypted:
            try:
                token = self.box.decrypt(target.refresh_token_encrypted)
            except SecretDecryptionError:
                token = None

        target.refresh_token_encrypted = None
        target.connection_status = PublishTargetConnectionStatus.DISCONNECTED
        target.is_active = False
        target.granted_scopes = None
        db.flush()

        revoked = False
        if token:
            try:
                revoked = self.oauth.revoke(token)
            except ProviderNotConfiguredError:
                revoked = False

        logger.info(
            "publish_target_disconnected",
            extra={"publish_target_id": str(target.id), "remote_revoked": revoked},
        )
        return target

    # ------------------------------------------------------------------- lookup

    @staticmethod
    def get(db: Session, target_id: uuid.UUID | str) -> PublishTarget | None:
        return db.query(PublishTarget).filter(PublishTarget.id == target_id).first()

    @staticmethod
    def serialize(target: PublishTarget) -> dict[str, Any]:
        """The API view. No credential field is reachable from here by construction.

        Written as an explicit allow-list rather than "everything except the token": a column
        added later is then invisible until someone decides it should be visible, which is
        the right default for a table that holds a secret.
        """
        return {
            "id": str(target.id),
            "platform": target.platform.value,
            "name": target.name,
            "channel_id": target.channel_id,
            "channel_title": target.channel_title,
            "is_active": target.is_active,
            "connection_status": target.connection_status.value,
            "is_publishable": target.is_publishable,
            "granted_scopes": (target.granted_scopes or "").split() or None,
            "connected_at": _iso(target.connected_at),
            "last_used_at": _iso(target.last_used_at),
            "last_error_code": target.last_error_code,
            "defaults": target.config_json or {},
            "created_at": _iso(target.created_at),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes where PostgreSQL returns aware ones."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
