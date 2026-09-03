"""The Google OAuth 2.0 authorization-code flow, and nothing else.

Written against the HTTP endpoints directly with ``httpx`` rather than pulling in
``google-auth`` / ``google-api-python-client``. Three reasons, in order of weight:

1. The failure classification in :mod:`app.publishing.youtube_publisher` depends on seeing
   the actual status code and the ``error.errors[].reason`` field. A client library that
   raises its own exception hierarchy puts a translation layer between this code and the
   only signal that distinguishes "retry" from "never retry".
2. ``google-api-python-client`` brings a large transitive tree (``httplib2``, its own auth
   stack) to make three POSTs and a resumable PUT loop.
3. Every request here goes through an injectable ``httpx.Client``, so the tests exercise the
   real request construction and the real response parsing against a mock transport, rather
   than asserting that a mocked library was called.

Nothing in this module logs a token, a code or a client secret.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.publishing.contracts import ProviderNotConfiguredError

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
CHANNELS_ENDPOINT = "https://www.googleapis.com/youtube/v3/channels"

# The minimum that permits an upload. Deliberately not `youtube` or `youtube.force-ssl`,
# which additionally grant reading and deleting playlists, comments and captions - none of
# which this system does. `youtube.readonly` is needed only to resolve which channel the
# token belongs to, which is what stops an operator publishing to the wrong channel.
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)

# How long an authorization URL stays completable.
STATE_TTL_MINUTES = 15

# Google error codes that mean the refresh token will never work again. Anything else is
# treated as transient, because guessing "permanent" wrongly locks a channel out.
UNRECOVERABLE_GRANT_ERRORS = frozenset(
    {"invalid_grant", "invalid_client", "unauthorized_client"}
)


class OAuthError(RuntimeError):
    """An OAuth exchange failed.

    ``code`` is the machine-readable ``error`` field from Google, which is the only part safe
    to store or show: the ``error_description`` can echo request parameters back.
    """

    def __init__(self, code: str, *, recoverable: bool = True) -> None:
        self.code = code
        self.recoverable = recoverable
        super().__init__(f"oauth failed: {code}")


@dataclass(frozen=True)
class TokenBundle:
    """The result of an exchange. ``refresh_token`` is present only on first consent."""

    access_token: str
    expires_at: datetime
    refresh_token: str | None = None
    granted_scopes: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # A dataclass repr would print both tokens the moment anything logs this object,
        # including a bare exception traceback that happens to hold it in a frame.
        return f"TokenBundle(expires_at={self.expires_at.isoformat()}, redacted)"


@dataclass(frozen=True)
class ChannelIdentity:
    channel_id: str
    channel_title: str | None


class YouTubeOAuthClient:
    def __init__(
        self,
        *,
        client_id: str | None,
        client_secret: str | None,
        redirect_uri: str | None,
        client: httpx.Client | None = None,
        timeout_sec: float = 20.0,
    ) -> None:
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        self._redirect_uri = (redirect_uri or "").strip()
        self._client = client
        self._timeout = timeout_sec

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self._redirect_uri)

    def require_configured(self) -> None:
        if not self.configured:
            missing = [
                name
                for name, value in (
                    ("YOUTUBE_CLIENT_ID", self._client_id),
                    ("YOUTUBE_CLIENT_SECRET", self._client_secret),
                    ("YOUTUBE_OAUTH_REDIRECT_URI", self._redirect_uri),
                )
                if not value
            ]
            raise ProviderNotConfiguredError(
                "YouTube OAuth is not configured; missing: " + ", ".join(missing)
            )

    @property
    def redirect_uri(self) -> str:
        return self._redirect_uri

    # ------------------------------------------------------------ authorization

    @staticmethod
    def new_state() -> str:
        """32 bytes of CSPRNG. Not a uuid4: uuid4 is for uniqueness, this is for secrecy."""
        return secrets.token_urlsafe(32)

    @staticmethod
    def state_expiry(now: datetime | None = None) -> datetime:
        return (now or datetime.now(timezone.utc)) + timedelta(minutes=STATE_TTL_MINUTES)

    def authorization_url(self, *, state: str) -> str:
        """Where to send the operator's browser."""
        self.require_configured()
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            # Without both of these Google returns a refresh token only on the very first
            # consent ever granted to this client, and a reconnect months later silently
            # yields an access token that expires in an hour and cannot be renewed.
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return str(httpx.URL(AUTH_ENDPOINT, params=params))

    # -------------------------------------------------------------- token calls

    def exchange_code(self, code: str) -> TokenBundle:
        """Trade the one-time authorization code for tokens."""
        self.require_configured()
        payload = {
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            # Must match the value the authorization used, byte for byte. Taken from
            # configuration, never rebuilt from the callback request.
            "redirect_uri": self._redirect_uri,
            "grant_type": "authorization_code",
        }
        data = self._post_token(payload)
        refresh_token = data.get("refresh_token")
        if not refresh_token:
            # An access token alone is useless to us: it expires within the hour and this
            # system publishes on a schedule measured in days. Failing here is better than
            # storing a target that appears connected and stops working by lunchtime.
            raise OAuthError("no_refresh_token_returned", recoverable=False)
        return TokenBundle(
            access_token=str(data["access_token"]),
            expires_at=_expiry_from(data),
            refresh_token=str(refresh_token),
            granted_scopes=data.get("scope"),
        )

    def refresh_access_token(self, refresh_token: str) -> TokenBundle:
        """Mint a short-lived access token from the stored refresh token."""
        self.require_configured()
        data = self._post_token(
            {
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
            }
        )
        return TokenBundle(
            access_token=str(data["access_token"]),
            expires_at=_expiry_from(data),
            # A refresh response does not re-issue the refresh token; the stored one stays.
            refresh_token=None,
            granted_scopes=data.get("scope"),
        )

    def revoke(self, refresh_token: str) -> bool:
        """Best-effort remote revocation on disconnect.

        Returns whether Google confirmed it. Deliberately non-fatal: local disconnection
        (dropping the ciphertext) is the part that must always happen, and a provider outage
        must not be able to keep a credential attached to a target the operator has said to
        detach.
        """
        try:
            response = self._http().post(
                REVOKE_ENDPOINT,
                data={"token": refresh_token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("youtube_revoke_failed", extra={"error_type": type(exc).__name__})
            return False
        return response.status_code == 200

    # ----------------------------------------------------------------- identity

    def resolve_channel(self, access_token: str) -> ChannelIdentity | None:
        """Ask the provider which channel this token actually reaches.

        Returns ``None`` when the account has no channel — a real case for a Google account
        that has never created one, and a much better answer than inventing an id.
        """
        response = self._http().get(
            CHANNELS_ENDPOINT,
            params={"part": "id,snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=self._timeout,
        )
        if response.status_code != 200:
            raise OAuthError(_error_code(response), recoverable=response.status_code >= 500)

        items = (response.json() or {}).get("items") or []
        if not items:
            return None
        item = items[0]
        snippet = item.get("snippet") or {}
        return ChannelIdentity(
            channel_id=str(item.get("id")),
            channel_title=(snippet.get("title") or None),
        )

    # ------------------------------------------------------------------ helpers

    def _post_token(self, payload: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._http().post(TOKEN_ENDPOINT, data=payload, timeout=self._timeout)
        except httpx.HTTPError as exc:
            # A network failure during a token call is safe to retry: no side effect has
            # been created on our side and none on theirs that matters.
            raise OAuthError(type(exc).__name__, recoverable=True) from exc

        if response.status_code != 200:
            code = _error_code(response)
            raise OAuthError(code, recoverable=code not in UNRECOVERABLE_GRANT_ERRORS)

        data = response.json() or {}
        if not data.get("access_token"):
            raise OAuthError("malformed_token_response", recoverable=False)
        return data

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=self._timeout)


def _expiry_from(data: dict[str, Any]) -> datetime:
    try:
        expires_in = int(data.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    return datetime.now(timezone.utc) + timedelta(seconds=expires_in)


def _error_code(response: httpx.Response) -> str:
    """The machine-readable error only.

    ``error_description`` and the response body are dropped on purpose: Google echoes request
    parameters into some of them, and this value is stored on the target and returned by the
    admin API.
    """
    try:
        body = response.json() or {}
    except ValueError:
        return f"http_{response.status_code}"

    error = body.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        errors = error.get("errors") or []
        if errors and isinstance(errors[0], dict) and errors[0].get("reason"):
            return str(errors[0]["reason"])
        if error.get("status"):
            return str(error["status"])
    return f"http_{response.status_code}"
