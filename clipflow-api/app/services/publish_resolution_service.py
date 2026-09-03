"""Settling attempts whose outcome the upload path could not determine.

An UNKNOWN attempt is a question: *is there a video on the channel for this?* This module is
the only place allowed to answer it, and it can answer in exactly two ways.

**Reconcile — ask the provider.** The resumable session can be probed: a zero-length PUT with
``Content-Range: bytes */<total>`` returns the finished video resource if the upload completed
after all. This is a real, protocol-supported answer, and it is the only automatic one that
exists. It works while the session is alive (Google keeps them about a week) and while we
still hold the session URI.

**What is deliberately NOT implemented.** There is no reliable way to search YouTube for "the
video we may have uploaded". ``search.list`` is eventually consistent, expensive in quota, and
matches on text that a legitimate second video could share. Embedding a private marker in the
description and grepping for it would work, but it means writing a tracking token into public
metadata for every video forever to solve a rare case — a hack, and §46 asks for none. So when
the session cannot answer, this refuses to guess and marks the attempt as needing a person.

**Resolve — a person answers.** An operator who has looked at the channel supplies the video
id. It is verified against the provider before being accepted: that it exists, and that it
belongs to the channel this target is connected to. An unverifiable id is not written.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.enums import (
    PipelineEventType,
    PipelineState,
    PublishAttemptStatus,
    PublishRetryability,
)
from app.models.publish_attempt import PublishAttempt
from app.publishing.youtube_oauth import OAuthError, YouTubeOAuthClient
from app.publishing.youtube_publisher import provider_reason
from app.security.secret_box import SecretDecryptionError, secret_box
from app.services import event_bus
from app.services.pipeline_state_machine import PipelineStateMachine
from app.services.publish_target_service import PublishTargetService

logger = logging.getLogger(__name__)

VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"


class ResolutionError(RuntimeError):
    """The requested resolution cannot be performed. Message is operator-facing."""


class PublishResolutionService:
    def __init__(
        self,
        *,
        targets: PublishTargetService | None = None,
        state_machine: PipelineStateMachine | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.targets = targets or PublishTargetService()
        self.state = state_machine or PipelineStateMachine()
        self._client = client

    # ---------------------------------------------------------------- reconcile

    def reconcile(self, db: Session, attempt: PublishAttempt) -> dict[str, Any]:
        """Try to settle an UNKNOWN attempt by asking the provider's own session."""
        if attempt.status != PublishAttemptStatus.UNKNOWN:
            raise ResolutionError(
                f"attempt is {attempt.status.value}; only an unknown attempt is reconciled"
            )

        session_uri = self._session_uri(attempt)
        if not session_uri:
            return self._needs_human(
                db, attempt,
                reason="no_resumable_session",
                detail=(
                    "the upload session was not retained, and there is no reliable way to "
                    "search the channel for this publication. Check the channel and use "
                    "/resolve with the video id, or mark it failed if no video exists."
                ),
            )

        try:
            video = self._probe(attempt, session_uri)
        except OAuthError as exc:
            raise ResolutionError(f"could not reach the provider: {exc.code}") from exc
        except httpx.HTTPError as exc:
            raise ResolutionError(
                f"could not reach the provider ({type(exc).__name__}); try again"
            ) from exc

        if video is None:
            return self._needs_human(
                db, attempt,
                reason="session_inconclusive",
                detail=(
                    "the upload session did not report a completed video. It may have "
                    "expired rather than failed, so this is not proof that nothing was "
                    "published — verify on the channel before republishing."
                ),
            )

        video_id = str(video.get("id") or "").strip()
        if not video_id:
            return self._needs_human(
                db, attempt, reason="session_returned_no_id",
                detail="the provider reported completion without a video id",
            )

        return self._accept(
            db, attempt, external_id=video_id, source="reconciled",
            provider_metadata=(video.get("status") or {}),
        )

    # ------------------------------------------------------------------ resolve

    def resolve(
        self, db: Session, attempt: PublishAttempt, *, external_id: str, verify: bool = True
    ) -> dict[str, Any]:
        """Accept an operator's answer, after checking it.

        ``verify`` exists only so a deployment with no provider access can still close a
        stale row; it is off the happy path and the caller has to ask for it explicitly. The
        default checks, because writing an unverified id would let a typo mark a run
        PUBLISHED against a video that is not ours.
        """
        if not attempt.needs_human:
            raise ResolutionError(
                f"attempt is {attempt.status.value}; only an unresolved attempt is resolved"
            )
        external_id = (external_id or "").strip()
        if not external_id:
            raise ResolutionError("external_id is required")

        provider_metadata: dict[str, Any] = {}
        if verify:
            video = self._fetch_video(attempt, external_id)
            if video is None:
                raise ResolutionError(
                    f"video {external_id} was not found on the provider; it cannot be "
                    "recorded as this publication"
                )
            snippet = video.get("snippet") or {}
            channel_id = str(snippet.get("channelId") or "")
            expected = attempt.target.channel_id if attempt.target else None
            if expected and channel_id and channel_id != expected:
                # The strongest check available: a video that belongs to another channel is
                # definitively not the one this attempt produced.
                raise ResolutionError(
                    f"video {external_id} belongs to channel {channel_id}, not to this "
                    f"target's channel {expected}"
                )
            provider_metadata = {
                "privacy_status": (video.get("status") or {}).get("privacyStatus"),
                "upload_status": (video.get("status") or {}).get("uploadStatus"),
                "channel_id": channel_id or None,
                "verified": True,
            }
        else:
            provider_metadata = {"verified": False}

        return self._accept(
            db, attempt, external_id=external_id, source="operator",
            provider_metadata=provider_metadata,
        )

    # -------------------------------------------------------- operator verdicts

    def mark_not_published(
        self, db: Session, attempt: PublishAttempt, *, note: str | None = None
    ) -> dict[str, Any]:
        """Record that an operator checked the channel and no video exists.

        Settles the attempt as a final failure rather than re-opening it for retry: a fresh
        publication of the same media is a deliberate act, requested by bumping the key's
        version, not a side effect of closing an investigation.
        """
        if not attempt.needs_human:
            raise ResolutionError(
                f"attempt is {attempt.status.value}; only an unresolved attempt is settled"
            )

        attempt.status = PublishAttemptStatus.FAILED_FINAL
        attempt.retryability = PublishRetryability.NOT_RETRYABLE
        attempt.error_code = attempt.error_code or "operator_confirmed_not_published"
        attempt.finished_at = datetime.now(timezone.utc)
        attempt.upload_session_uri_encrypted = None
        provider_metadata = dict(attempt.provider_metadata_json or {})
        provider_metadata["operator_note"] = note
        attempt.provider_metadata_json = provider_metadata
        db.flush()

        self._release_job(db, attempt)
        self._emit(db, attempt, "publish.resolved", {"resolution": "not_published"})
        db.commit()
        return {"status": attempt.status.value, "external_id": None,
                "resolution": "not_published"}

    def cancel(self, db: Session, attempt: PublishAttempt) -> dict[str, Any]:
        """Cancel an attempt that has not started sending bytes.

        Only from PENDING. Cancelling anything further along would be a lie: this row does
        not control the video, and marking it CANCELED would suggest the publication was
        undone when nothing was removed from YouTube. Deleting or unpublishing a live video
        is out of scope for this PR.
        """
        if attempt.status != PublishAttemptStatus.PENDING:
            raise ResolutionError(
                f"attempt is {attempt.status.value}; only a pending attempt can be canceled. "
                "Canceling does not remove anything already published."
            )
        attempt.status = PublishAttemptStatus.CANCELED
        attempt.retryability = PublishRetryability.NOT_RETRYABLE
        attempt.finished_at = datetime.now(timezone.utc)
        db.flush()
        self._release_job(db, attempt)
        self._emit(db, attempt, "publish.canceled", {})
        db.commit()
        return {"status": attempt.status.value}

    # ------------------------------------------------------------------ helpers

    def _accept(
        self,
        db: Session,
        attempt: PublishAttempt,
        *,
        external_id: str,
        source: str,
        provider_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        attempt.status = PublishAttemptStatus.SUCCEEDED
        attempt.retryability = PublishRetryability.NOT_RETRYABLE
        attempt.external_id = external_id
        attempt.external_id_source = source
        attempt.finished_at = attempt.finished_at or datetime.now(timezone.utc)
        attempt.error_code = None
        attempt.upload_session_uri_encrypted = None
        merged = dict(attempt.provider_metadata_json or {})
        merged.update(provider_metadata)
        attempt.provider_metadata_json = merged
        db.flush()

        self._settle_job(db, attempt)
        self._emit(
            db, attempt, "publish.succeeded",
            {"external_id": external_id, "external_id_source": source},
        )
        db.commit()
        return {
            "status": attempt.status.value,
            "external_id": external_id,
            "external_url": f"https://www.youtube.com/watch?v={external_id}",
            "resolution": source,
        }

    def _needs_human(
        self, db: Session, attempt: PublishAttempt, *, reason: str, detail: str
    ) -> dict[str, Any]:
        attempt.status = PublishAttemptStatus.NEEDS_MANUAL_RESOLUTION
        attempt.retryability = PublishRetryability.REQUIRES_MANUAL_RESOLUTION
        attempt.error_code = reason
        attempt.error_message = detail
        db.flush()
        self._emit(db, attempt, "publish.unknown", {"reason": reason},
                   event_type=PipelineEventType.WARNING)
        db.commit()
        return {"status": attempt.status.value, "reason": reason, "detail": detail,
                "external_id": None}

    def _settle_job(self, db: Session, attempt: PublishAttempt) -> None:
        """Promote the run to PUBLISHED if this was the last outstanding publication."""
        job = attempt.job
        if job is None:
            return

        siblings = (
            db.query(PublishAttempt)
            .filter(PublishAttempt.pipeline_job_id == job.id)
            .all()
        )
        all_done = all(a.status == PublishAttemptStatus.SUCCEEDED for a in siblings)

        metadata = dict(job.metadata_json or {})
        metadata["publication_status"] = "published" if all_done else "partial"
        job.metadata_json = metadata

        if all_done:
            if job.state == PipelineState.READY_TO_PUBLISH:
                # The run was released when the ambiguous attempt ended; claim it again so
                # the transition into PUBLISHED is the legal sequential one.
                self.state.start_publishing(db, job, actor="resolution")
            if job.state == PipelineState.PUBLISHING:
                self.state.mark_published(
                    db, job,
                    external_ids=[a.external_id for a in siblings if a.external_id],
                    service="publisher",
                )
        db.flush()

    def _release_job(self, db: Session, attempt: PublishAttempt) -> None:
        job = attempt.job
        if job is not None and job.state == PipelineState.PUBLISHING:
            self.state.publish_failed(db, job, reason="attempt_resolved_without_publication")

    def _probe(self, attempt: PublishAttempt, session_uri: str) -> dict[str, Any] | None:
        total = int(attempt.media_bytes or 0)
        response = self._http().put(
            session_uri,
            headers={"Content-Length": "0", "Content-Range": f"bytes */{total}"},
            timeout=30.0,
        )
        if response.status_code in (200, 201):
            return response.json() or {}
        if response.status_code == 308:
            # The session is alive but incomplete: the upload genuinely did not finish, so
            # nothing was published under it.
            return None
        if response.status_code in (404, 410):
            # Expired. Says nothing either way about whether a video exists.
            return None
        raise OAuthError(provider_reason(response), recoverable=response.status_code >= 500)

    def _fetch_video(self, attempt: PublishAttempt, video_id: str) -> dict[str, Any] | None:
        target = attempt.target
        if target is None:
            raise ResolutionError("attempt has no target to verify against")

        credential = self.targets.credential_for(target)
        oauth = YouTubeOAuthClient(
            client_id=credential.client_id,
            client_secret=credential.client_secret,
            redirect_uri=settings.youtube_oauth_redirect_uri,
            client=self._client,
        )
        try:
            token = oauth.refresh_access_token(credential.refresh_token)
        except OAuthError as exc:
            raise ResolutionError(f"could not authenticate to verify: {exc.code}") from exc

        response = self._http().get(
            VIDEOS_ENDPOINT,
            params={"part": "snippet,status", "id": video_id},
            headers={"Authorization": f"Bearer {token.access_token}"},
            timeout=30.0,
        )
        if response.status_code != 200:
            raise ResolutionError(f"provider rejected the lookup: {provider_reason(response)}")
        items = (response.json() or {}).get("items") or []
        return items[0] if items else None

    def _emit(
        self,
        db: Session,
        attempt: PublishAttempt,
        stage: str,
        extra: dict[str, Any],
        *,
        event_type: PipelineEventType = PipelineEventType.INFO,
    ) -> None:
        event_bus.publish_event(
            db,
            service="publisher",
            event_type=event_type,
            pipeline_job_id=attempt.pipeline_job_id,
            stage=stage,
            message=f"{stage} for attempt {attempt.id}",
            payload={
                "pipeline_job_id": str(attempt.pipeline_job_id),
                "publish_attempt_id": str(attempt.id),
                "publish_target_id": str(attempt.target_id),
                "provider": "youtube",
                "media_identity": attempt.media_identity,
                **extra,
            },
        )

    @staticmethod
    def _session_uri(attempt: PublishAttempt) -> str | None:
        if not attempt.upload_session_uri_encrypted:
            return None
        try:
            return secret_box.decrypt(attempt.upload_session_uri_encrypted)
        except SecretDecryptionError:
            return None

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=30.0)


def serialize_attempt(attempt: PublishAttempt) -> dict[str, Any]:
    """The API view of an attempt.

    An explicit allow-list. ``upload_session_uri_encrypted`` is absent by construction rather
    than by a filter someone could forget to update: the session URI is a bearer credential
    for this upload, and it looks like a harmless URL.
    """
    return {
        "id": str(attempt.id),
        "pipeline_job_id": str(attempt.pipeline_job_id),
        "publish_target_id": str(attempt.target_id),
        "provider": attempt.target.platform.value if attempt.target else None,
        "media_identity": attempt.media_identity,
        "media_storage_key": attempt.media_storage_key,
        "media_bytes": attempt.media_bytes,
        "status": attempt.status.value,
        "retryability": attempt.retryability.value if attempt.retryability else None,
        "attempt_no": attempt.attempt_no,
        "max_attempts": attempt.max_attempts,
        "external_id": attempt.external_id,
        "external_url": (
            f"https://www.youtube.com/watch?v={attempt.external_id}"
            if attempt.external_id else None
        ),
        "external_id_source": attempt.external_id_source,
        "started_at": _iso(attempt.started_at),
        "finished_at": _iso(attempt.finished_at),
        "error_code": attempt.error_code,
        "error_message": attempt.error_message,
        "bytes_uploaded": attempt.bytes_uploaded,
        "has_resumable_session": bool(attempt.upload_session_uri_encrypted),
        "metadata_snapshot": (attempt.payload_json or {}).get("metadata"),
        "provider_metadata": attempt.provider_metadata_json or {},
        "created_at": _iso(attempt.created_at),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
