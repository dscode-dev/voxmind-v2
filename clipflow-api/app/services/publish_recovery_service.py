"""Deciding what to do with a publication whose worker died.

**The distinction this module exists for.** A queue lease answers *which worker owns this
command*. It does not answer *is it safe to upload again*. Those are two different authorities
and deriving the second from the first is the bug that produces duplicate videos:

    lease expired  →  requeue  →  new upload  →  second video on the channel

So a recovered command is never simply re-run. The lease expiry only makes the command
available again; what happens next is decided here, from evidence written to the attempt row
before each irreversible step.

**The evidence, and what each combination proves.**

``provider_started_at`` is committed immediately before the first call that could create
anything at YouTube, and cleared whenever the attempt settles. ``upload_session_uri_encrypted``
holds a resumable session. ``bytes_uploaded`` records how far the last execution got.

    no provider_started_at, no session   nothing remote happened      safe to run again
    session present                      a session exists             probe it
    provider_started_at, no session      a session POST may have run  safe: a session with
                                          but returned nothing        no bytes is not a video
    no session, bytes_uploaded > 0       contradictory                fail safe: UNKNOWN

Probing a session is a real answer, not a guess: the resumable protocol returns the finished
video resource if the upload completed, or the committed offset if it did not.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.enums import (
    PipelineEventType,
    PublishAttemptStatus,
    PublishRetryability,
)
from app.models.publish_attempt import PublishAttempt
from app.publishing.youtube_publisher import provider_reason
from app.security.secret_box import SecretDecryptionError, secret_box
from app.services import event_bus

logger = logging.getLogger(__name__)

# What recovery decided. Strings, so they land in events and logs unchanged.
REQUEUE = "requeue"
RESUME = "resume"
COMPLETED = "completed"
AMBIGUOUS = "ambiguous"
NOT_STUCK = "not_stuck"
# The provider could not be reached to ask. Not an answer, so the row is left exactly as it
# is and the next sweep asks again - marking it UNKNOWN would turn a network blip into work
# for a human.
UNDETERMINED = "undetermined"


class RecoveryDecision:
    def __init__(self, action: str, *, detail: str, external_id: str | None = None) -> None:
        self.action = action
        self.detail = detail
        self.external_id = external_id

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "detail": self.detail,
                "external_id": self.external_id}


class PublishRecoveryService:
    """Classifies abandoned publications. Never uploads anything itself."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def recover(
        self, db: Session, attempt: PublishAttempt, *, worker_id: str
    ) -> RecoveryDecision:
        """Decide what may happen to an attempt whose executor is gone."""
        if attempt.status != PublishAttemptStatus.IN_PROGRESS:
            return RecoveryDecision(NOT_STUCK, detail=f"attempt is {attempt.status.value}")

        session_uri = self._session_uri(attempt)
        bytes_uploaded = int(attempt.bytes_uploaded or 0)

        if session_uri:
            decision = self._probe(attempt, session_uri, bytes_uploaded)
        elif attempt.provider_started_at is None and bytes_uploaded == 0:
            # The strongest case: this execution provably never reached the provider.
            decision = RecoveryDecision(
                REQUEUE, detail="no provider call was started; nothing exists remotely"
            )
        elif bytes_uploaded == 0:
            # A session POST may have been sent and its answer lost. That call creates an
            # upload session, not a video, and an orphaned session simply expires - so
            # starting a fresh one cannot duplicate anything.
            decision = RecoveryDecision(
                REQUEUE,
                detail="a session may have been opened but no bytes were sent; "
                       "an unused session cannot become a video",
            )
        else:
            # Bytes went somewhere and we no longer know where. Fail safe.
            decision = RecoveryDecision(
                AMBIGUOUS,
                detail=f"{bytes_uploaded} bytes were sent with no session to probe",
            )

        self._apply(db, attempt, decision, worker_id=worker_id)
        return decision

    # ------------------------------------------------------------------- probe

    def _probe(
        self, attempt: PublishAttempt, session_uri: str, bytes_uploaded: int
    ) -> RecoveryDecision:
        total = int(attempt.media_bytes or 0)
        try:
            response = self._http().put(
                session_uri,
                headers={"Content-Length": "0", "Content-Range": f"bytes */{total}"},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            # Could not ask. Not an answer either way, so the row is left untouched and the
            # next sweep asks again.
            return RecoveryDecision(
                UNDETERMINED,
                detail=f"the session could not be probed ({type(exc).__name__}); "
                       "will re-check",
            )

        if response.status_code in (200, 201):
            video = response.json() or {}
            video_id = str(video.get("id") or "").strip()
            if video_id:
                # The happy ending: the upload finished after the worker died.
                return RecoveryDecision(
                    COMPLETED, detail="the session reports a completed video",
                    external_id=video_id,
                )
            return RecoveryDecision(
                AMBIGUOUS, detail="the session reported completion without a video id"
            )

        if response.status_code == 308:
            # Alive and incomplete: the remaining bytes can be sent to the same session, so
            # no second video can result.
            offset = _offset_from(response)
            return RecoveryDecision(
                RESUME, detail=f"the session is resumable from byte {offset}"
            )

        if response.status_code in (404, 410):
            if bytes_uploaded > 0:
                # Expired after bytes were sent. Expiry is not proof that nothing was
                # published, and this is exactly the final-chunk case.
                return RecoveryDecision(
                    AMBIGUOUS,
                    detail="the session expired after bytes were sent; a video may exist",
                )
            return RecoveryDecision(
                REQUEUE, detail="the session expired before any bytes were sent"
            )

        return RecoveryDecision(
            AMBIGUOUS,
            detail=f"the session answered {provider_reason(response)}",
        )

    # ------------------------------------------------------------------- apply

    def _apply(
        self,
        db: Session,
        attempt: PublishAttempt,
        decision: RecoveryDecision,
        *,
        worker_id: str,
    ) -> None:
        now = datetime.now(timezone.utc)

        if decision.action == UNDETERMINED:
            # Deliberately nothing. The attempt stays IN_PROGRESS with its session intact,
            # which is the truthful description of what we know.
            return

        if decision.action == COMPLETED:
            attempt.status = PublishAttemptStatus.SUCCEEDED
            attempt.retryability = PublishRetryability.NOT_RETRYABLE
            attempt.external_id = decision.external_id
            attempt.external_id_source = "recovered"
            attempt.finished_at = now
            attempt.upload_session_uri_encrypted = None
            attempt.provider_started_at = None
            stage, event_type = "publish.recovered", PipelineEventType.INFO

        elif decision.action == AMBIGUOUS:
            attempt.status = PublishAttemptStatus.UNKNOWN
            attempt.retryability = PublishRetryability.REQUIRES_MANUAL_RESOLUTION
            attempt.error_code = "worker_lost_during_upload"
            attempt.error_message = decision.detail
            attempt.finished_at = now
            attempt.provider_started_at = None
            stage, event_type = "publish.unknown", PipelineEventType.ERROR

        else:
            # REQUEUE and RESUME both return the attempt to the executable set. The
            # difference is not in the status - it is that RESUME still holds the session
            # URI, so the next execution continues it rather than opening a new one.
            #
            # A conditional UPDATE, so a worker that is somehow still alive and finishing
            # this upload cannot have it taken away underneath it: the status must still be
            # IN_PROGRESS for the release to apply.
            values: dict[str, object] = {
                "status": PublishAttemptStatus.PENDING,
                "provider_started_at": None,
                "claimed_at": None,
                "publisher_worker_id": None,
            }
            if decision.action == REQUEUE:
                # The session, if there was one, is unusable: expired, or never opened.
                # Keeping a dead URI would make the next execution try to resume into
                # nothing instead of opening a fresh session.
                values["upload_session_uri_encrypted"] = None
                values["bytes_uploaded"] = None

            released = db.execute(
                update(PublishAttempt)
                .where(
                    PublishAttempt.id == attempt.id,
                    PublishAttempt.status == PublishAttemptStatus.IN_PROGRESS,
                )
                .values(
                    **values,
                    # Not counted as a spent attempt: nothing was published, and charging
                    # the budget for a process death would exhaust it on infrastructure.
                    attempt_no=PublishAttempt.attempt_no - 1,
                    # The command has to be sent again; the sweep looks for exactly this.
                    enqueued_at=None,
                )
            )
            db.commit()
            if released.rowcount != 1:
                logger.info(
                    "publish_recovery_release_skipped",
                    extra={"publish_attempt_id": str(attempt.id)},
                )
                return
            db.refresh(attempt)
            stage, event_type = "publish.recovered", PipelineEventType.WARNING

        db.flush()
        event_bus.publish_event(
            db,
            service="publisher",
            event_type=event_type,
            pipeline_job_id=attempt.pipeline_job_id,
            stage=stage,
            message=f"recovery: {decision.action}",
            payload={
                "pipeline_job_id": str(attempt.pipeline_job_id),
                "publish_attempt_id": str(attempt.id),
                "publish_target_id": str(attempt.target_id),
                "publisher_worker_id": worker_id,
                "provider": "youtube",
                "attempt_no": attempt.attempt_no,
                "recovery_action": decision.action,
                "detail": decision.detail,
                "external_id": decision.external_id,
            },
        )
        db.commit()

        logger.info(
            "publish_recovered",
            extra={
                "publish_attempt_id": str(attempt.id),
                "pipeline_job_id": str(attempt.pipeline_job_id),
                "publisher_worker_id": worker_id,
                "recovery_action": decision.action,
                "resume_offset": attempt.bytes_uploaded,
                "external_id": decision.external_id,
            },
        )

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _session_uri(attempt: PublishAttempt) -> str | None:
        if not attempt.upload_session_uri_encrypted:
            return None
        try:
            return secret_box.decrypt(attempt.upload_session_uri_encrypted)
        except SecretDecryptionError:
            # A session we cannot read is a session we cannot probe. It is NOT treated as
            # "no session": bytes may have gone to it.
            return None

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=30.0)


def _offset_from(response: httpx.Response) -> int:
    header = response.headers.get("range") or response.headers.get("Range")
    if not header:
        return 0
    try:
        return int(header.split("-")[-1]) + 1
    except (ValueError, IndexError):
        return 0
