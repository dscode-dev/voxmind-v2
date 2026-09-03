"""The YouTube Data API v3 adapter: resumable upload, and the classification of what failed.

Two things in here carry the weight of this PR.

**Resumable upload, streamed.** ``videos.insert`` with ``uploadType=resumable`` is a two-step
protocol: a POST that carries only the metadata and returns a session URI, then PUTs of byte
ranges to that URI. A final clip is tens or hundreds of megabytes, so it is read in chunks
from a file handle and never held in memory, and an interrupted upload can resume from the
offset the server reports rather than starting again.

**The ambiguity window.** Between "the last byte left this process" and "a response arrived"
there is an interval in which the video may already exist on YouTube. Nothing in the API
closes it: there is no idempotency key for ``videos.insert``, so a retry after a lost response
uploads a second video. Every code path here that can end inside that window resolves to
``UNKNOWN`` — never to a failure, because a failure invites a retry and a retry is exactly
what must not happen.

The distinction is *where* a request died, not which exception it raised:

* a timeout while opening the session — nothing was created, safe to retry;
* a timeout while sending a middle chunk — resumable, the offset can be queried;
* a timeout while sending the **final** chunk — the video may exist. UNKNOWN.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.models.enums import PublishRetryability
from app.publishing.contracts import (
    PublishOutcome,
    PublishRequest,
    PublishResult,
)
from app.publishing.youtube_oauth import OAuthError, YouTubeOAuthClient

logger = logging.getLogger(__name__)

PROVIDER = "youtube"
UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

# The API requires every non-final chunk to be a multiple of 256 KiB.
CHUNK_MULTIPLE = 256 * 1024

# Provider reasons that will fail identically no matter how many times they are sent.
# Everything not listed is treated as transient: over-classifying as permanent strands a
# publication that would have worked, which is the more expensive mistake here.
NON_RETRYABLE_REASONS = frozenset(
    {
        "invalidTitle",
        "invalidDescription",
        "invalidTags",
        "invalidCategoryId",
        "invalidVideoMetadata",
        "invalidFilename",
        "mediaBodyRequired",
        "invalidRequest",
        "forbidden",
        "youtubeSignupRequired",
        "uploadLimitExceeded",
        "failedPrecondition",
        "badRequest",
    }
)

# quotaExceeded is deliberately absent from both sets and handled explicitly: the daily
# project quota resets, and rateLimitExceeded is a matter of seconds. Neither is permanent,
# but neither should be hammered either - see _classify_reason.
RATE_LIMITED_REASONS = frozenset({"quotaExceeded", "rateLimitExceeded", "userRateLimitExceeded"})


class YouTubePublisher:
    """Implements the ``Publisher`` port for YouTube."""

    provider = PROVIDER

    def __init__(
        self,
        *,
        oauth: YouTubeOAuthClient,
        client: httpx.Client | None = None,
        timeout_sec: float = 900.0,
        chunk_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self._oauth = oauth
        self._client = client
        self._timeout = timeout_sec
        # Rounded down to the required multiple, floor one chunk: a value the API rejects
        # would fail every upload identically, and the caller supplying MiB should not have
        # to know about the 256 KiB rule.
        self._chunk_bytes = max(CHUNK_MULTIPLE, (chunk_bytes // CHUNK_MULTIPLE) * CHUNK_MULTIPLE)

    # ------------------------------------------------------------------- publish

    def publish(self, request: PublishRequest) -> PublishResult:
        try:
            token = self._oauth.refresh_access_token(request.credential.refresh_token)
        except OAuthError as exc:
            # Credentials fail before any byte moves, so this can never be ambiguous.
            return PublishResult(
                provider=PROVIDER,
                outcome=PublishOutcome.FAILED,
                retryability=(
                    PublishRetryability.RETRYABLE
                    if exc.recoverable
                    else PublishRetryability.NOT_RETRYABLE
                ),
                error_code=exc.code,
                error_message="oauth refresh failed",
            )

        session_uri = request.resume_session_uri
        bytes_uploaded = 0

        if session_uri is None:
            try:
                session_uri = self._open_session(request, token.access_token)
                # Persisted before a single byte is sent: from here on there is something at
                # the provider that a recovery must probe rather than replace.
                request.report_progress(session_uri, 0)
            except _ProviderFailure as failure:
                return failure.as_result()
            except httpx.HTTPError as exc:
                # Nothing was created: the session POST either reached Google or it did not,
                # and a session with no bytes is not a video.
                return PublishResult(
                    provider=PROVIDER,
                    outcome=PublishOutcome.FAILED,
                    retryability=PublishRetryability.RETRYABLE,
                    error_code=type(exc).__name__,
                    error_message="failed to open upload session",
                    bytes_uploaded=0,
                )
        else:
            # Resuming: ask the server what it already has rather than assuming.
            try:
                probe = self._probe_offset(session_uri, request.media.size_bytes)
            except _ProviderFailure as failure:
                return failure.as_result(session_uri=session_uri)
            except httpx.HTTPError as exc:
                return PublishResult(
                    provider=PROVIDER,
                    outcome=PublishOutcome.FAILED,
                    retryability=PublishRetryability.RETRYABLE,
                    error_code=type(exc).__name__,
                    error_message="failed to probe resumable session",
                    session_uri=session_uri,
                )
            if probe.completed_video is not None:
                # The session had already finished: this is the happy resolution of a
                # previous ambiguous ending, and it is why probing beats retrying.
                return self._success(probe.completed_video, request, request.media.size_bytes,
                                     session_uri)
            bytes_uploaded = probe.offset

        return self._upload(request, session_uri, bytes_uploaded)

    # ------------------------------------------------------------------ session

    def _open_session(self, request: PublishRequest, access_token: str) -> str:
        body = _video_resource(request)
        response = self._http().post(
            UPLOAD_ENDPOINT,
            params={"part": "snippet,status", "uploadType": "resumable"},
            json=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-Upload-Content-Length": str(request.media.size_bytes),
                "X-Upload-Content-Type": request.media.content_type,
            },
            timeout=self._timeout,
        )
        if response.status_code not in (200, 201):
            raise _ProviderFailure.from_response(response)

        location = response.headers.get("location") or response.headers.get("Location")
        if not location:
            raise _ProviderFailure(
                error_code="missing_session_uri",
                retryability=PublishRetryability.RETRYABLE,
                message="provider accepted the session request without returning a location",
            )
        return location

    # ------------------------------------------------------------------- upload

    def _upload(
        self, request: PublishRequest, session_uri: str, start_offset: int
    ) -> PublishResult:
        total = request.media.size_bytes
        offset = start_offset

        try:
            handle = request.media.open()
        except OSError as exc:
            return PublishResult(
                provider=PROVIDER,
                outcome=PublishOutcome.FAILED,
                retryability=PublishRetryability.NOT_RETRYABLE,
                error_code=type(exc).__name__,
                error_message="final media could not be opened",
                session_uri=session_uri,
            )

        try:
            if offset:
                handle.seek(offset)

            while offset < total:
                chunk = handle.read(self._chunk_bytes)
                if not chunk:
                    # The object is shorter than its declared length. Sending a truncated
                    # body would have the provider reject or, worse, accept a broken video.
                    return PublishResult(
                        provider=PROVIDER,
                        outcome=PublishOutcome.FAILED,
                        retryability=PublishRetryability.NOT_RETRYABLE,
                        error_code="media_shorter_than_declared",
                        error_message=f"stream ended at {offset} of {total} bytes",
                        bytes_uploaded=offset,
                        session_uri=session_uri,
                    )

                end = offset + len(chunk) - 1
                is_final_chunk = end >= total - 1

                try:
                    response = self._http().put(
                        session_uri,
                        content=chunk,
                        headers={
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{end}/{total}",
                        },
                        timeout=self._timeout,
                    )
                except httpx.HTTPError as exc:
                    # THE decision of this module. A dropped connection on a middle chunk
                    # left no video behind and the session can be resumed. The same drop on
                    # the final chunk may have delivered every byte, in which case YouTube
                    # is finalising a video we will never hear about.
                    if is_final_chunk:
                        return PublishResult(
                            provider=PROVIDER,
                            outcome=PublishOutcome.UNKNOWN,
                            error_code=type(exc).__name__,
                            error_message=(
                                "connection lost after sending the final chunk; the video "
                                "may exist"
                            ),
                            bytes_uploaded=end + 1,
                            session_uri=session_uri,
                        )
                    return PublishResult(
                        provider=PROVIDER,
                        outcome=PublishOutcome.FAILED,
                        retryability=PublishRetryability.RETRYABLE,
                        error_code=type(exc).__name__,
                        error_message="connection lost mid-upload; session is resumable",
                        bytes_uploaded=offset,
                        session_uri=session_uri,
                    )

                # 308 Resume Incomplete: the normal answer to a non-final chunk.
                if response.status_code == 308:
                    offset = _next_offset(response, fallback=end + 1)
                    # Committed as we go, so a worker killed on the next chunk leaves an
                    # accurate resume point instead of an unknown one.
                    request.report_progress(session_uri, offset)
                    continue

                if response.status_code in (200, 201):
                    video = response.json() or {}
                    return self._success(video, request, total, session_uri)

                failure = _ProviderFailure.from_response(response)
                if is_final_chunk and response.status_code >= 500:
                    # A 5xx on the final chunk is genuinely ambiguous: the server may have
                    # persisted the video and failed while responding.
                    return PublishResult(
                        provider=PROVIDER,
                        outcome=PublishOutcome.UNKNOWN,
                        error_code=failure.error_code,
                        error_message=(
                            "provider returned a server error on the final chunk; the video "
                            "may exist"
                        ),
                        bytes_uploaded=end + 1,
                        session_uri=session_uri,
                    )
                return failure.as_result(bytes_uploaded=offset, session_uri=session_uri)

            # Every byte was accounted for and the provider never sent a completion body.
            # Not a success (there is no video id) and not a clean failure either.
            return PublishResult(
                provider=PROVIDER,
                outcome=PublishOutcome.UNKNOWN,
                error_code="no_completion_response",
                error_message="all bytes were sent but the provider returned no video",
                bytes_uploaded=total,
                session_uri=session_uri,
            )
        finally:
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - a failed close must not mask the outcome
                logger.warning("youtube_media_close_failed")

    # ------------------------------------------------------------------- resume

    def _probe_offset(self, session_uri: str, total: int) -> "_Probe":
        """Ask the session how much it already holds.

        A zero-length PUT with ``Content-Range: bytes */<total>`` is the protocol's way to
        query, and it is the only safe move when reconnecting: it can also answer "already
        finished", which turns an ambiguous ending into a confirmed one.
        """
        response = self._http().put(
            session_uri,
            headers={"Content-Length": "0", "Content-Range": f"bytes */{total}"},
            timeout=self._timeout,
        )
        if response.status_code in (200, 201):
            return _Probe(offset=total, completed_video=response.json() or {})
        if response.status_code == 308:
            return _Probe(offset=_next_offset(response, fallback=0), completed_video=None)
        raise _ProviderFailure.from_response(response)

    # ------------------------------------------------------------------ results

    def _success(
        self, video: dict[str, Any], request: PublishRequest, total: int, session_uri: str
    ) -> PublishResult:
        video_id = str(video.get("id") or "").strip()
        if not video_id:
            # A 200 with no id is not a success we can record; without an external id there
            # is nothing to reconcile against later.
            return PublishResult(
                provider=PROVIDER,
                outcome=PublishOutcome.UNKNOWN,
                error_code="missing_video_id",
                error_message="provider reported success without a video id",
                bytes_uploaded=total,
                session_uri=session_uri,
            )

        status = video.get("status") or {}
        return PublishResult(
            provider=PROVIDER,
            outcome=PublishOutcome.SUCCEEDED,
            external_id=video_id,
            external_url=WATCH_URL.format(video_id=video_id),
            published_at=datetime.now(timezone.utc),
            privacy=status.get("privacyStatus") or request.metadata.privacy,
            bytes_uploaded=total,
            provider_metadata={
                "upload_status": status.get("uploadStatus"),
                "privacy_status": status.get("privacyStatus"),
                "made_for_kids": status.get("madeForKids"),
                # Present when YouTube rejected the content after accepting the bytes.
                "rejection_reason": status.get("rejectionReason"),
            },
        )

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=self._timeout, follow_redirects=False)


# --------------------------------------------------------------------- internals


class _Probe:
    def __init__(self, *, offset: int, completed_video: dict[str, Any] | None) -> None:
        self.offset = offset
        self.completed_video = completed_video


class _ProviderFailure(Exception):
    """A provider response that is definitely not a success and definitely not ambiguous."""

    def __init__(
        self,
        *,
        error_code: str,
        retryability: PublishRetryability,
        message: str,
    ) -> None:
        self.error_code = error_code
        self.retryability = retryability
        self.message = message
        super().__init__(f"{error_code}: {message}")

    @classmethod
    def from_response(cls, response: httpx.Response) -> "_ProviderFailure":
        reason = provider_reason(response)
        return cls(
            error_code=reason,
            retryability=_classify(response.status_code, reason),
            message=f"provider returned {response.status_code}",
        )

    def as_result(
        self, *, bytes_uploaded: int | None = None, session_uri: str | None = None
    ) -> PublishResult:
        return PublishResult(
            provider=PROVIDER,
            outcome=PublishOutcome.FAILED,
            retryability=self.retryability,
            error_code=self.error_code,
            error_message=self.message,
            bytes_uploaded=bytes_uploaded,
            session_uri=session_uri,
        )


def _classify(status_code: int, reason: str) -> PublishRetryability:
    """Which failures may be sent again.

    Status code first for the unambiguous bands, then the provider's own reason, because a
    403 is ``quotaExceeded`` (come back later) as often as it is ``forbidden`` (never).
    """
    if reason in NON_RETRYABLE_REASONS:
        return PublishRetryability.NOT_RETRYABLE
    if reason in RATE_LIMITED_REASONS:
        # Retryable, but the caller's backoff is what makes that true; hammering a quota
        # error just burns the rest of it.
        return PublishRetryability.RETRYABLE
    if status_code in (401, 403):
        # Credentials or permissions: refreshing already happened, so this will not fix
        # itself on the next attempt.
        return PublishRetryability.NOT_RETRYABLE
    if status_code == 404:
        # The resumable session expired. The upload can start over from a new session.
        return PublishRetryability.RETRYABLE
    if status_code == 429 or status_code >= 500:
        return PublishRetryability.RETRYABLE
    if 400 <= status_code < 500:
        return PublishRetryability.NOT_RETRYABLE
    return PublishRetryability.RETRYABLE


def provider_reason(response: httpx.Response) -> str:
    """The provider's machine-readable reason. Never the message body."""
    try:
        body = response.json() or {}
    except ValueError:
        return f"http_{response.status_code}"

    error = body.get("error") or {}
    if isinstance(error, str):
        return error
    errors = error.get("errors") or []
    if errors and isinstance(errors[0], dict) and errors[0].get("reason"):
        return str(errors[0]["reason"])
    if error.get("status"):
        return str(error["status"])
    return f"http_{response.status_code}"


def _next_offset(response: httpx.Response, *, fallback: int) -> int:
    """Where the server says to continue from.

    ``Range: bytes=0-<last>`` is inclusive, so the next byte is last + 1. A 308 with no Range
    header means the server holds nothing yet.
    """
    header = response.headers.get("range") or response.headers.get("Range")
    if not header:
        return 0 if fallback is None else fallback
    try:
        return int(header.split("-")[-1]) + 1
    except (ValueError, IndexError):
        return fallback


def _video_resource(request: PublishRequest) -> dict[str, Any]:
    """The ``videos.insert`` body, built only from the frozen metadata snapshot."""
    metadata = request.metadata
    snippet: dict[str, Any] = {
        "title": metadata.title,
        "description": metadata.description,
        "tags": list(metadata.tags),
    }
    if metadata.category_id:
        snippet["categoryId"] = str(metadata.category_id)
    if metadata.language:
        snippet["defaultLanguage"] = metadata.language
        snippet["defaultAudioLanguage"] = metadata.language

    return {
        "snippet": snippet,
        "status": {
            "privacyStatus": metadata.privacy,
            # Required by the API since the COPPA changes; it is a product/legal declaration,
            # so it comes from configuration and is never inferred from the content.
            "selfDeclaredMadeForKids": bool(metadata.made_for_kids),
        },
    }
