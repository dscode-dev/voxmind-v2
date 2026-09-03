"""The publication boundary: validate, commit an attempt, upload, record what happened.

This is the only code that decides whether something may be published. The adapter knows how
to talk to YouTube; the state machine knows which transitions are legal; this knows the rules.

**Ordering is the design, and it is the opposite of admission's.** Admission commits a row
then enqueues, because a lost message is recoverable. Here the row is committed *before* the
first byte and the external id is written *after* the provider confirms, because the thing
that must never happen is a video existing that this system has no record of. Every crash
point leaves something an operator can act on:

    crash before the attempt commits   nothing was sent            nothing to do
    crash mid-upload                   attempt IN_PROGRESS         resumable, session kept
    crash after bytes, before response attempt UNKNOWN             human resolves it
    crash after response, before commit attempt IN_PROGRESS        reconcile finds the video

**UNKNOWN is a full stop.** Not a retry with a longer backoff, not a "probably failed". A
publication whose outcome is unknown stays unknown until a person or a session probe settles
it, because the alternative is a duplicate video on a public channel.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import redis
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.enums import (
    PipelineEventType,
    PipelineState,
    PublishAttemptStatus,
    PublishRetryability,
)
from app.models.pipeline_job import PipelineJob
from app.models.publish_attempt import PublishAttempt
from app.models.publish_target import PublishTarget
from app.publishing.contracts import (
    ProviderNotConfiguredError,
    PublishMedia,
    PublishRequest,
    PublishResult,
)
from app.publishing.media_source import MediaUnavailableError, MinioMediaSource
from app.publishing.publish_queue import PublishQueue, command_payload
from app.publishing.metadata import MetadataValidationError, ResolvedMetadata, resolve
from app.publishing.youtube_oauth import YouTubeOAuthClient
from app.publishing.youtube_publisher import YouTubePublisher
from app.services import event_bus
from app.services.artifact_content_service import ArtifactContentService
from app.services.pipeline_state_machine import PipelineStateMachine
from app.services.publish_target_service import (
    PublishTargetService,
    TargetNotPublishableError,
)
from app.security.secret_box import SecretDecryptionError, secret_box

logger = logging.getLogger(__name__)

# Blocked reasons, as machine-readable codes. Every one of them is a refusal to publish, and
# every refusal names itself so an operator is never told only "no".
GLOBAL_DISABLED = "publishing_disabled"
TARGET_DISABLED = "target_disabled"
TARGET_RECONNECT_REQUIRED = "target_reconnect_required"
TARGET_NO_CREDENTIAL = "target_no_credential"
PROVIDER_NOT_CONFIGURED = "provider_not_configured"
NOT_READY = "job_not_ready_to_publish"
NOT_ELIGIBLE = "publication_not_eligible"
ELIGIBILITY_MISSING = "publication_eligibility_missing"
NO_MEDIA = "no_publishable_media"
MEDIA_UNAVAILABLE = "final_media_unavailable"
METADATA_INVALID = "metadata_invalid"
ALREADY_PUBLISHED = "already_published"
ATTEMPT_UNRESOLVED = "attempt_requires_manual_resolution"
ATTEMPT_FINAL = "attempt_failed_final"
ATTEMPTS_EXHAUSTED = "attempts_exhausted"
# Another request holds this publication right now. Not an error and not a retry:
# whoever claimed it is uploading, and a second uploader would be a second video.
ATTEMPT_IN_PROGRESS = "attempt_in_progress"

# Bounded, because an unbounded retry against a quota-limited API is a way to lose the quota
# rather than to publish. UNKNOWN never enters this count - it is not a retry path at all.
DEFAULT_MAX_ATTEMPTS = 3

# The only two statuses a publisher may pick up and run.
#
# Everything else is excluded for a specific reason: SUCCEEDED and FAILED_FINAL are settled;
# CANCELED was withdrawn; UNKNOWN may already exist at the provider and is the one state that
# must never be executed automatically; IN_PROGRESS is either live or a crash to be classified
# from evidence, never simply repeated.
EXECUTABLE_STATUSES = (
    PublishAttemptStatus.PENDING,
    PublishAttemptStatus.FAILED_RETRYABLE,
)


@dataclass
class MediaItem:
    """One publishable output of a run, as described by publish_package.json."""

    identity: str
    storage_key: str
    video_index: int
    video: dict[str, Any]


@dataclass
class ItemOutcome:
    media_identity: str
    status: str
    attempt_id: str | None = None
    external_id: str | None = None
    external_url: str | None = None
    error_code: str | None = None
    retryability: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "media_identity": self.media_identity,
            "status": self.status,
            "attempt_id": self.attempt_id,
            "external_id": self.external_id,
            "external_url": self.external_url,
            "error_code": self.error_code,
            "retryability": self.retryability,
            "blocked_by": self.blocked_by,
            "notes": self.notes,
        }


@dataclass
class PublishReport:
    pipeline_job_id: str
    publish_target_id: str | None
    dry_run: bool
    status: str = "blocked"
    blocked_by: list[str] = field(default_factory=list)
    items: list[ItemOutcome] = field(default_factory=list)
    publication_status: str = "none"
    job_state: str | None = None
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "pipeline_job_id": self.pipeline_job_id,
            "publish_target_id": self.publish_target_id,
            "dry_run": self.dry_run,
            "status": self.status,
            "blocked_by": self.blocked_by,
            "publication_status": self.publication_status,
            "job_state": self.job_state,
            "items": [item.as_dict() for item in self.items],
            "duration_ms": self.duration_ms,
        }


class PublishingService:
    def __init__(
        self,
        *,
        publisher: Any = None,
        targets: PublishTargetService | None = None,
        media_source: MinioMediaSource | None = None,
        artifacts: ArtifactContentService | None = None,
        state_machine: PipelineStateMachine | None = None,
        queue: PublishQueue | None = None,
        session_factory=None,
    ) -> None:
        self.targets = targets or PublishTargetService()
        self.media_source = media_source or MinioMediaSource()
        self.artifacts = artifacts or ArtifactContentService()
        self.state = state_machine or PipelineStateMachine()
        self.queue = queue or PublishQueue()
        # Only used by the progress recorder, which needs a session of its own while the
        # caller's transaction is open. Injectable so a test can point it at the same
        # database the test is reading.
        self._session_factory = session_factory
        self._publisher = publisher

    # ------------------------------------------------------------------ publish

    def publish(
        self,
        db: Session,
        *,
        job: PipelineJob,
        target: PublishTarget,
        dry_run: bool = True,
        overrides: dict[str, Any] | None = None,
        media_selection: list[int] | None = None,
        actor: str | None = None,
    ) -> PublishReport:
        """Validate, then (unless this is a dry run) upload each selected media item."""
        started = time.monotonic()
        report = PublishReport(
            pipeline_job_id=str(job.id),
            publish_target_id=str(target.id),
            dry_run=dry_run,
            job_state=job.state.value,
        )

        blocked_by = self._preflight(job, target)
        if blocked_by:
            report.blocked_by = blocked_by
            report.status = "blocked"
            report.duration_ms = _elapsed(started)
            self._emit(db, job, "publish.blocked", report, target=target,
                       event_type=PipelineEventType.WARNING)
            return report

        try:
            items = self.resolve_media(db, job, selection=media_selection)
        except MediaUnavailableError as exc:
            report.blocked_by = [MEDIA_UNAVAILABLE]
            report.status = "blocked"
            report.items = [ItemOutcome(media_identity="*", status="blocked",
                                        blocked_by=[MEDIA_UNAVAILABLE],
                                        notes=[str(exc)])]
            report.duration_ms = _elapsed(started)
            self._emit(db, job, "publish.blocked", report, target=target,
                       event_type=PipelineEventType.WARNING)
            return report

        if not items:
            report.blocked_by = [NO_MEDIA]
            report.status = "blocked"
            report.duration_ms = _elapsed(started)
            self._emit(db, job, "publish.blocked", report, target=target,
                       event_type=PipelineEventType.WARNING)
            return report

        package = self._package(job)
        for item in items:
            report.items.append(
                self._publish_item(
                    db, job=job, target=target, item=item, package=package,
                    overrides=overrides, dry_run=dry_run, actor=actor,
                )
            )

        report.publication_status = self._publication_status(report.items)
        report.status = "validated" if dry_run else "accepted"

        if not dry_run:
            # The run enters PUBLISHING on acceptance rather than when the first byte moves.
            # Between the two there is a queue, and a run sitting in READY_TO_PUBLISH with a
            # command already in flight would invite a second publish request.
            if any(item.status == "queued" for item in report.items):
                if job.state == PipelineState.READY_TO_PUBLISH:
                    self.state.start_publishing(db, job, actor=actor)
            self._settle_job(db, job)
            report.publication_status = job_publication_status(db, job)

        report.job_state = job.state.value
        report.duration_ms = _elapsed(started)
        return report

    # ------------------------------------------------------------------- enqueue

    def enqueue_attempt(self, db: Session, attempt: PublishAttempt) -> bool:
        """Put this attempt's command on the queue and record that it got there.

        Ordering is the same trade admission makes, for the same reason: the row commits
        first, so the failure mode is a command that was never sent (recoverable by a sweep)
        rather than a command referring to a row that does not exist.
        """
        payload = command_payload(
            publish_attempt_id=str(attempt.id),
            pipeline_job_id=str(attempt.pipeline_job_id),
            target_id=str(attempt.target_id),
            media_identity=attempt.media_identity or "",
        )
        try:
            self.queue.enqueue(payload)
        except redis.RedisError as exc:
            # Deliberately not fatal. The attempt stays committed with enqueued_at NULL,
            # which is exactly what the sweep looks for; failing the request here would
            # leave the operator thinking nothing happened when a publication is pending.
            logger.warning(
                "publish_enqueue_failed",
                extra={"publish_attempt_id": str(attempt.id),
                       "error_type": type(exc).__name__},
            )
            return False

        attempt.enqueued_at = datetime.now(timezone.utc)
        db.commit()
        return True

    def sweep_pending_enqueue(self, db: Session, *, limit: int = 20) -> int:
        """Queue commands for attempts that committed but never reached Redis.

        **This is not autopublish.** It only ever looks at PublishAttempt rows, which exist
        only because someone explicitly asked to publish. It never queries PipelineJob for
        runs in READY_TO_PUBLISH, and it never creates an attempt - doing either would make
        the system start publishing on its own, which is precisely what this PR must not do.
        """
        stuck = (
            db.query(PublishAttempt)
            .filter(
                PublishAttempt.enqueued_at.is_(None),
                PublishAttempt.status.in_(EXECUTABLE_STATUSES),
            )
            .order_by(PublishAttempt.created_at.asc())
            .limit(max(1, limit))
            .all()
        )
        recovered = 0
        for attempt in stuck:
            if self.enqueue_attempt(db, attempt):
                recovered += 1
                logger.info(
                    "publish_enqueue_recovered",
                    extra={"publish_attempt_id": str(attempt.id),
                           "pipeline_job_id": str(attempt.pipeline_job_id)},
                )
        return recovered

    # --------------------------------------------------------------- validation

    def _preflight(self, job: PipelineJob, target: PublishTarget) -> list[str]:
        """Every reason this publication must not happen. All of them, not just the first.

        Returning the full list matters: an operator who fixes one blocker and is then told
        about the next one learns the system is unreliable, when it was only terse.
        """
        blocked: list[str] = []

        # 1. The global switch. Checked first and checked for dry runs too, because a
        #    deployment that has not opted in should not be validating publications either.
        if not settings.publishing_enabled:
            blocked.append(GLOBAL_DISABLED)

        # 2. The target's own switches.
        if not target.is_active:
            blocked.append(TARGET_DISABLED)
        if target.connection_status.value == "reconnect_required":
            blocked.append(TARGET_RECONNECT_REQUIRED)
        elif not target.refresh_token_encrypted:
            blocked.append(TARGET_NO_CREDENTIAL)

        # 3. The workflow state. PUBLISHED is not an error worth alarming on, but it is
        #    still a refusal: re-publishing a finished run means a second video.
        if job.state == PipelineState.PUBLISHED:
            blocked.append(ALREADY_PUBLISHED)
        elif job.state not in (PipelineState.READY_TO_PUBLISH, PipelineState.PUBLISHING):
            blocked.append(NOT_READY)

        # 4. The QA verdict, re-read from the run rather than trusted via its state. The
        #    state says where the workflow is; eligibility says whether the *output* may be
        #    published, and a manual endpoint must not be able to bypass it by arriving at a
        #    run whose state was set some other way.
        metadata = job.metadata_json or {}
        eligibility = metadata.get("publication_eligibility")
        if not isinstance(eligibility, dict) or not eligibility:
            # Fail-closed: an unmeasured gate is not a passed gate.
            blocked.append(ELIGIBILITY_MISSING)
        elif not eligibility.get("eligible"):
            blocked.append(NOT_ELIGIBLE)

        # 5. The provider must actually be configured; otherwise this would fail after
        #    creating an attempt row.
        try:
            self.targets.oauth.require_configured()
        except ProviderNotConfiguredError:
            blocked.append(PROVIDER_NOT_CONFIGURED)
        if target.refresh_token_encrypted and not secret_box.available:
            blocked.append(PROVIDER_NOT_CONFIGURED)

        return blocked

    # -------------------------------------------------------------------- media

    def resolve_media(
        self, db: Session, job: PipelineJob, *, selection: list[int] | None = None
    ) -> list[MediaItem]:
        """Which files this run publishes.

        **Cardinality is the package's, not ours.** ``publish_package.json`` carries a
        ``videos[]`` array where each entry has its own title, description and hashtags and
        its own rendered clip. That is the existing contract for "one publishable output", so
        each entry is one publication. ``final_reel.mp4`` is the concatenation used for
        review; it has no per-video editorial metadata and is not published here.
        """
        package = self._package(job)
        if not package:
            raise MediaUnavailableError(
                f"publish_package.json not readable for job {job.worker_job_id}"
            )

        prefix = f"jobs/{job.worker_job_id}"
        wanted = set(selection) if selection else None
        items: list[MediaItem] = []

        for index, video in enumerate(package.get("videos") or [], start=1):
            video_index = int(video.get("video_index") or index)
            if wanted is not None and video_index not in wanted:
                continue

            clip = video.get("final_clip") or {}
            file_name = clip.get("file_name")
            if not file_name or clip.get("status") != "generated":
                # A missing clip is skipped rather than blocking its siblings: two good
                # videos should not be held back by a third that failed to render.
                continue

            identity = f"final_clips/{file_name}"
            items.append(
                MediaItem(
                    identity=identity,
                    storage_key=f"{prefix}/{identity}",
                    video_index=video_index,
                    video=video,
                )
            )

        if selection and not items:
            raise MediaUnavailableError(
                f"none of the requested video indexes {sorted(set(selection))} are generated"
            )
        return items

    def _package(self, job: PipelineJob) -> dict[str, Any]:
        data = self.artifacts.load_json(f"jobs/{job.worker_job_id}/publish_package.json")
        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------------- one item

    def _publish_item(
        self,
        db: Session,
        *,
        job: PipelineJob,
        target: PublishTarget,
        item: MediaItem,
        package: dict[str, Any],
        overrides: dict[str, Any] | None,
        dry_run: bool,
        actor: str | None,
    ) -> ItemOutcome:
        key = idempotency_key(job.id, target.id, item.identity)

        try:
            resolved = resolve(
                video=item.video,
                package=package,
                target_config=target.config_json or {},
                overrides=overrides,
            )
        except MetadataValidationError as exc:
            return ItemOutcome(
                media_identity=item.identity, status="blocked",
                blocked_by=[METADATA_INVALID], notes=exc.problems,
            )

        existing = (
            db.query(PublishAttempt).filter(PublishAttempt.idempotency_key == key).first()
        )

        # A settled attempt is the answer, whatever the caller asked for. This is what makes
        # a duplicated request idempotent rather than merely unlikely to duplicate.
        if existing is not None:
            settled = self._settled_outcome(existing, item)
            if settled is not None:
                return settled

        try:
            size = self.media_source.stat(item.storage_key)
        except MediaUnavailableError as exc:
            return ItemOutcome(
                media_identity=item.identity, status="blocked",
                blocked_by=[MEDIA_UNAVAILABLE], notes=[str(exc)],
            )

        if dry_run:
            return ItemOutcome(
                media_identity=item.identity,
                status="would_publish",
                attempt_id=str(existing.id) if existing else None,
                notes=[
                    f"idempotency_key={key}",
                    f"media_bytes={size}",
                    f"privacy={resolved.metadata.privacy}",
                    *resolved.notes,
                ],
            )

        attempt = existing or self._create_attempt(
            db, job=job, target=target, item=item, key=key, size=size, resolved=resolved
        )

        # Re-checked here and not only above: _create_attempt can return a row this request
        # did not create, because it lost the insert race. That row may already be finished.
        settled = self._settled_outcome(attempt, item)
        if settled is not None:
            return settled

        if attempt.status == PublishAttemptStatus.IN_PROGRESS:
            # A publisher holds it. Enqueueing again would put a duplicate command behind an
            # upload that is already in flight.
            return ItemOutcome(
                media_identity=item.identity, status="in_progress",
                attempt_id=str(attempt.id), blocked_by=[ATTEMPT_IN_PROGRESS],
                notes=["a publisher is already uploading this media"],
            )

        if (
            attempt.attempt_no >= attempt.max_attempts
            and attempt.status == PublishAttemptStatus.FAILED_RETRYABLE
        ):
            return ItemOutcome(
                media_identity=item.identity, status="blocked", attempt_id=str(attempt.id),
                blocked_by=[ATTEMPTS_EXHAUSTED], error_code=attempt.error_code,
            )

        # Where the upload used to be. The command goes on the queue and this request
        # returns; a publisher process claims it, takes the atomic DB claim, and uploads.
        #
        # A PENDING attempt that already carries a command is not given a second one. A
        # FAILED_RETRYABLE one always is: its previous command has been settled - retried,
        # exhausted, or dead-lettered - so an operator asking again would otherwise get a
        # silent no-op. A duplicate command is harmless here in a way silence is not, because
        # the atomic claim still allows exactly one upload.
        needs_command = (
            attempt.enqueued_at is None
            or attempt.status == PublishAttemptStatus.FAILED_RETRYABLE
        )
        queued = self.enqueue_attempt(db, attempt) if needs_command else True

        self._emit(db, job, "publish.queued", None, target=target, attempt=attempt)
        db.commit()

        return ItemOutcome(
            media_identity=item.identity,
            status="queued" if queued else "pending_enqueue",
            attempt_id=str(attempt.id),
            notes=(
                [f"idempotency_key={key}"] if queued
                else ["the queue was unreachable; a sweep will send this command"]
            ),
        )

    # ------------------------------------------------------------------ execute

    def execute_attempt(
        self,
        db: Session,
        *,
        attempt: PublishAttempt,
        worker_id: str,
    ) -> ItemOutcome:
        """Run one publication. The publisher process entry point.

        Everything the upload needs is on the attempt row: the frozen metadata snapshot, the
        media key, and any resumable session left by a previous execution. The queue command
        carries four ids and nothing else, so nothing here can be stale relative to what was
        decided when the publication was accepted.
        """
        job = attempt.job
        target = attempt.target
        item = MediaItem(
            identity=attempt.media_identity or "",
            storage_key=attempt.media_storage_key or "",
            video_index=int((attempt.payload_json or {}).get("video_index") or 1),
            video={},
        )

        if job is None or target is None:
            return ItemOutcome(
                media_identity=item.identity, status="blocked", attempt_id=str(attempt.id),
                blocked_by=["attempt_orphaned"],
            )

        # The redelivery path. A command whose outcome was committed before its ACK arrives
        # again, finds the row terminal, and goes no further: provider calls delta zero.
        settled = self._settled_outcome(attempt, item)
        if settled is not None:
            return settled

        if attempt.status not in EXECUTABLE_STATUSES:
            # IN_PROGRESS reaches here only through recovery, which classifies it from
            # evidence rather than from the queue lease. It is never simply run again.
            return ItemOutcome(
                media_identity=item.identity, status="not_executable",
                attempt_id=str(attempt.id), blocked_by=[f"attempt_{attempt.status.value}"],
            )

        blocked_by = self._runtime_preflight(target)
        if blocked_by:
            # Paused, not failed: the switch may be flipped back, and spending an attempt on
            # a policy decision would exhaust the budget for no reason.
            return ItemOutcome(
                media_identity=item.identity, status="paused",
                attempt_id=str(attempt.id), blocked_by=blocked_by,
            )

        if (
            attempt.attempt_no >= attempt.max_attempts
            and attempt.status == PublishAttemptStatus.FAILED_RETRYABLE
        ):
            return ItemOutcome(
                media_identity=item.identity, status="blocked", attempt_id=str(attempt.id),
                blocked_by=[ATTEMPTS_EXHAUSTED], error_code=attempt.error_code,
            )

        # Still required, for the same reason as before the queue existed: at-least-once
        # delivery means a command can arrive twice, and the queue cannot prevent that. The
        # database can.
        if not self._claim(db, attempt, worker_id=worker_id):
            db.refresh(attempt)
            settled = self._settled_outcome(attempt, item)
            if settled is not None:
                return settled
            return ItemOutcome(
                media_identity=item.identity, status="in_progress",
                attempt_id=str(attempt.id), blocked_by=[ATTEMPT_IN_PROGRESS],
                notes=["another publisher holds this attempt"],
            )

        outcome = self._upload(
            db, job=job, target=target, item=item, attempt=attempt,
            resolved=None, size=attempt.media_bytes or 0, actor=worker_id,
        )
        self._settle_job(db, job)
        return outcome

    @staticmethod
    def _runtime_preflight(target: PublishTarget) -> list[str]:
        """The checks that must still hold at execution time, not only at accept time.

        A kill switch flipped or a target disconnected between accepting a command and
        running it has to stop the upload. Deliberately not the full preflight: that also
        checks the run workflow state, which is legitimately PUBLISHING by now.
        """
        blocked: list[str] = []
        if not settings.publishing_enabled:
            blocked.append(GLOBAL_DISABLED)
        if not target.is_active:
            blocked.append(TARGET_DISABLED)
        if target.connection_status.value == "reconnect_required":
            blocked.append(TARGET_RECONNECT_REQUIRED)
        elif not target.refresh_token_encrypted:
            blocked.append(TARGET_NO_CREDENTIAL)
        return blocked

    @staticmethod
    def _claim(db: Session, attempt: PublishAttempt, *, worker_id: str | None = None) -> bool:
        """Take exclusive ownership of this publication, or report that someone else has it.

        A conditional UPDATE, so the database decides. ``WHERE status IN (claimable)`` is the
        whole mechanism: the row moves to IN_PROGRESS exactly once per attempt, and the
        loser's UPDATE matches zero rows rather than overwriting the winner's claim.

        A SELECT-then-UPDATE here would reintroduce the race it exists to close.
        """
        claimable = (
            PublishAttemptStatus.PENDING,
            PublishAttemptStatus.FAILED_RETRYABLE,
            # A row left IN_PROGRESS by a crashed process is deliberately NOT claimable: its
            # upload may still be in flight somewhere, which is the ambiguous case.
        )
        now = datetime.now(timezone.utc)
        result = db.execute(
            update(PublishAttempt)
            .where(
                PublishAttempt.id == attempt.id,
                PublishAttempt.status.in_(claimable),
            )
            .values(
                status=PublishAttemptStatus.IN_PROGRESS,
                attempt_no=PublishAttempt.attempt_no + 1,
                started_at=now,
                claimed_at=now,
                publisher_worker_id=worker_id,
                error_code=None,
                error_message=None,
                # Cleared on every claim: it means "this execution has reached the
                # provider", and a stale value from a previous attempt would make a fresh
                # execution look like it had already done something remotely.
                provider_started_at=None,
            )
        )
        db.commit()
        if result.rowcount != 1:
            logger.info(
                "publish_attempt_already_claimed",
                extra={"publish_attempt_id": str(attempt.id)},
            )
            return False
        db.refresh(attempt)
        return True

    def _settled_outcome(self, attempt: PublishAttempt, item: MediaItem) -> ItemOutcome | None:
        """The answer for an attempt that no further upload may change."""
        if attempt.status == PublishAttemptStatus.SUCCEEDED:
            return ItemOutcome(
                media_identity=item.identity, status="already_published",
                attempt_id=str(attempt.id), external_id=attempt.external_id,
                external_url=_watch_url(attempt.external_id),
            )
        if attempt.needs_human:
            # THE invariant. An attempt whose outcome is unknown is never uploaded again by
            # any code path, including a fresh operator request: the video may already exist.
            return ItemOutcome(
                media_identity=item.identity, status="requires_manual_resolution",
                attempt_id=str(attempt.id), blocked_by=[ATTEMPT_UNRESOLVED],
                error_code=attempt.error_code,
                retryability=PublishRetryability.REQUIRES_MANUAL_RESOLUTION.value,
            )
        if attempt.status == PublishAttemptStatus.FAILED_FINAL:
            return ItemOutcome(
                media_identity=item.identity, status="blocked", attempt_id=str(attempt.id),
                blocked_by=[ATTEMPT_FINAL], error_code=attempt.error_code,
                retryability=PublishRetryability.NOT_RETRYABLE.value,
            )
        if attempt.status == PublishAttemptStatus.CANCELED:
            return ItemOutcome(
                media_identity=item.identity, status="canceled", attempt_id=str(attempt.id),
            )
        return None

    def _create_attempt(
        self,
        db: Session,
        *,
        job: PipelineJob,
        target: PublishTarget,
        item: MediaItem,
        key: str,
        size: int,
        resolved: ResolvedMetadata,
    ) -> PublishAttempt:
        """Commit the attempt before anything is sent.

        The unique index on ``idempotency_key`` is what settles a race, not the SELECT above
        it: two concurrent requests both see nothing, both insert, and exactly one commits.
        The loser re-reads the winner's row instead of starting a second upload.
        """
        attempt = PublishAttempt(
            pipeline_job_id=job.id,
            target_id=target.id,
            idempotency_key=key,
            media_identity=item.identity,
            media_storage_key=item.storage_key,
            media_bytes=size,
            status=PublishAttemptStatus.PENDING,
            attempt_no=0,
            max_attempts=DEFAULT_MAX_ATTEMPTS,
            payload_json={
                "metadata": resolved.as_snapshot(),
                "video_index": item.video_index,
                # So a later reader knows which contract produced this snapshot without
                # having to date it against the git history.
                "publish_contract_version": "publish-01",
                "frozen_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        try:
            with db.begin_nested():
                db.add(attempt)
        except IntegrityError:
            # The savepoint rollback has already detached the pending row, so expunging it
            # again raises InvalidRequestError - which would turn losing a benign race into
            # a 500 for the operator whose request was the correct, deduplicated one.
            # Found by the concurrency smoke; the unique index was doing its job and the
            # recovery path was not.
            if attempt in db:
                db.expunge(attempt)
            winner = (
                db.query(PublishAttempt)
                .filter(PublishAttempt.idempotency_key == key)
                .one()
            )
            logger.info(
                "publish_attempt_race_lost",
                extra={"publish_attempt_id": str(winner.id), "idempotency_key": key},
            )
            return winner
        db.commit()
        db.refresh(attempt)
        return attempt

    # ------------------------------------------------------------------- upload

    def _upload(
        self,
        db: Session,
        *,
        job: PipelineJob,
        target: PublishTarget,
        item: MediaItem,
        attempt: PublishAttempt,
        resolved: ResolvedMetadata,
        size: int,
        actor: str | None,
    ) -> ItemOutcome:
        try:
            credential = self.targets.credential_for(target)
        except TargetNotPublishableError as exc:
            return ItemOutcome(
                media_identity=item.identity, status="blocked", attempt_id=str(attempt.id),
                blocked_by=[TARGET_NO_CREDENTIAL], notes=[str(exc)],
            )
        except SecretDecryptionError:
            return ItemOutcome(
                media_identity=item.identity, status="blocked", attempt_id=str(attempt.id),
                blocked_by=[TARGET_NO_CREDENTIAL],
            )

        if job.state == PipelineState.READY_TO_PUBLISH:
            self.state.start_publishing(db, job, actor=actor)

        # The metadata that was frozen when this attempt was created, not the one just
        # resolved: a retry must send what the first try sent, or the same logical
        # publication would exist under two different titles depending on timing.
        snapshot = (attempt.payload_json or {}).get("metadata") or resolved.as_snapshot()
        metadata = _metadata_from_snapshot(snapshot, fallback=resolved)

        # Status, attempt_no and started_at were set by _claim; setting them again here
        # would let a caller that skipped the claim look like it had one.
        self._emit(db, job, "publish.started", None, target=target, attempt=attempt)

        resume_uri = self._resume_uri(attempt)

        # Committed BEFORE the provider is touched, and this is the whole point of the
        # column: if this process dies now, recovery can tell that something may have
        # reached YouTube. Recording it afterwards would leave exactly the window it exists
        # to close.
        attempt.provider_started_at = datetime.now(timezone.utc)
        db.commit()

        started = time.monotonic()

        try:
            with self.media_source.download(item.storage_key) as local:
                request = PublishRequest(
                    attempt_id=str(attempt.id),
                    pipeline_job_id=str(job.id),
                    publish_target_id=str(target.id),
                    media=PublishMedia(
                        identity=item.identity,
                        storage_key=item.storage_key,
                        size_bytes=local.size_bytes,
                        content_type=local.content_type,
                        open_stream=lambda path=local.path: open(path, "rb"),
                    ),
                    metadata=metadata,
                    credential=credential,
                    resume_session_uri=resume_uri,
                    on_progress=self._progress_recorder(attempt.id),
                )
                result = self.publisher().publish(request)
        except MediaUnavailableError as exc:
            result = PublishResult(
                provider="youtube", outcome="failed",
                retryability=PublishRetryability.NOT_RETRYABLE,
                error_code="final_media_unavailable", error_message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            # An unexpected error in our own code, before or around the upload. Not
            # classified as ambiguous: the adapter owns that judgement and would have
            # returned UNKNOWN itself if bytes had been at risk. Only the type is kept - an
            # exception message here can contain a URL carrying a session token.
            logger.exception(
                "publish_upload_crashed",
                extra={"publish_attempt_id": str(attempt.id), "pipeline_job_id": str(job.id)},
            )
            result = PublishResult(
                provider="youtube", outcome="failed",
                retryability=PublishRetryability.RETRYABLE,
                error_code=type(exc).__name__, error_message="publisher raised",
            )

        duration_ms = _elapsed(started)
        outcome = self._record(db, job=job, target=target, attempt=attempt, result=result,
                               duration_ms=duration_ms, item=item, worker_id=actor)
        db.commit()
        return outcome

    def _record(
        self,
        db: Session,
        *,
        job: PipelineJob,
        target: PublishTarget,
        attempt: PublishAttempt,
        result: PublishResult,
        duration_ms: int,
        item: MediaItem,
        worker_id: str | None = None,
    ) -> ItemOutcome:
        """Write the outcome down. The only place attempt status is decided.

        **Guarded against a resurrected worker.** A process that stalled long enough for its
        lease to expire, was recovered, and then woke up still holds a live database session
        and an in-flight result. Without this check it would write that stale result over the
        outcome the worker that actually finished the job recorded - turning a SUCCEEDED
        publication into a FAILED_RETRYABLE one, and inviting a retry that duplicates a video
        that already exists.

        The queue's ownership token stops such a worker acknowledging a command; this stops
        it corrupting the row. They are separate guards because they protect separate things.
        """
        if worker_id is not None:
            current = (
                db.query(PublishAttempt.publisher_worker_id, PublishAttempt.status)
                .filter(PublishAttempt.id == attempt.id)
                .first()
            )
            if current is not None and (
                current[0] != worker_id
                or current[1] != PublishAttemptStatus.IN_PROGRESS
            ):
                logger.warning(
                    "publish_outcome_discarded_not_owner",
                    extra={
                        "publish_attempt_id": str(attempt.id),
                        "publisher_worker_id": worker_id,
                        "current_owner": current[0],
                        "current_status": current[1].value if current[1] else None,
                    },
                )
                db.rollback()
                return ItemOutcome(
                    media_identity=item.identity, status="superseded",
                    attempt_id=str(attempt.id),
                    notes=["another publisher settled this attempt while this one was "
                           "stalled; the stale result was discarded"],
                )

        now = datetime.now(timezone.utc)
        attempt.bytes_uploaded = result.bytes_uploaded
        attempt.provider_metadata_json = result.provider_metadata or None
        # This execution is over however it ended, so the "may have reached the provider"
        # flag stops applying. Leaving it set would make a later retry look, to recovery,
        # like it had already touched YouTube when it had not yet started.
        attempt.provider_started_at = None
        if result.session_uri:
            attempt.upload_session_uri_encrypted = _encrypt_session(result.session_uri)

        if result.succeeded:
            attempt.status = PublishAttemptStatus.SUCCEEDED
            attempt.retryability = PublishRetryability.NOT_RETRYABLE
            attempt.external_id = result.external_id
            attempt.external_id_source = "provider"
            attempt.finished_at = now
            # The session is spent and it is a credential; there is no reason to keep it.
            attempt.upload_session_uri_encrypted = None
            target.last_used_at = now
            db.flush()

            self._emit(db, job, "publish.succeeded", None, target=target, attempt=attempt,
                       extra={"duration_ms": duration_ms, "external_id": result.external_id})
            _log(job, target, attempt, result, duration_ms, "succeeded")
            return ItemOutcome(
                media_identity=item.identity, status="published", attempt_id=str(attempt.id),
                external_id=result.external_id,
                external_url=result.external_url or _watch_url(result.external_id),
            )

        if result.is_unknown:
            # No retryability is assigned on purpose: there is no safe automatic next step,
            # and leaving the column null makes any code that tries to branch on it fail
            # loudly rather than pick a default.
            attempt.status = PublishAttemptStatus.UNKNOWN
            attempt.retryability = PublishRetryability.REQUIRES_MANUAL_RESOLUTION
            attempt.error_code = result.error_code
            attempt.error_message = result.error_message
            attempt.finished_at = now
            db.flush()

            self._emit(db, job, "publish.unknown", None, target=target, attempt=attempt,
                       event_type=PipelineEventType.ERROR,
                       extra={"duration_ms": duration_ms, "error_code": result.error_code})
            _log(job, target, attempt, result, duration_ms, "unknown")
            return ItemOutcome(
                media_identity=item.identity, status="unknown", attempt_id=str(attempt.id),
                error_code=result.error_code,
                retryability=PublishRetryability.REQUIRES_MANUAL_RESOLUTION.value,
                notes=["outcome ambiguous; no automatic retry will be attempted"],
            )

        retryable = result.retryability == PublishRetryability.RETRYABLE
        attempt.status = (
            PublishAttemptStatus.FAILED_RETRYABLE if retryable
            else PublishAttemptStatus.FAILED_FINAL
        )
        attempt.retryability = result.retryability
        attempt.error_code = result.error_code
        attempt.error_message = result.error_message
        attempt.finished_at = now

        # A credential the provider rejected stops being used everywhere, not just here.
        if self.targets.is_credential_error(result.error_code):
            self.targets.mark_reconnect_required(db, target, error_code=result.error_code or "")
        db.flush()

        self._emit(db, job, "publish.failed", None, target=target, attempt=attempt,
                   event_type=PipelineEventType.ERROR,
                   extra={"duration_ms": duration_ms, "error_code": result.error_code,
                          "retryability": attempt.retryability.value if attempt.retryability else None})
        _log(job, target, attempt, result, duration_ms, "failed")
        return ItemOutcome(
            media_identity=item.identity, status="failed", attempt_id=str(attempt.id),
            error_code=result.error_code,
            retryability=attempt.retryability.value if attempt.retryability else None,
        )

    # ------------------------------------------------------------ job settlement

    def _settle_job(self, db: Session, job: PipelineJob) -> None:
        """Move the run according to every attempt it has, read from the database.

        **Rewritten for the async runtime.** It used to judge the run from the items in the
        report it had just produced. That was correct while one request published every clip
        in a loop; with a queue, a publisher executes exactly one attempt and sees exactly
        one item, so a run with three clips would have been marked PUBLISHED by whichever
        one happened to finish first. The siblings still queued would have been forgotten.

        The attempts table is the only complete answer, so it is the one consulted.
        """
        attempts = (
            db.query(PublishAttempt)
            .filter(PublishAttempt.pipeline_job_id == job.id)
            .all()
        )
        if not attempts:
            return

        succeeded = [a for a in attempts if a.status == PublishAttemptStatus.SUCCEEDED]
        status = attempts_publication_status(attempts)

        if status == "published":
            if job.state == PipelineState.READY_TO_PUBLISH:
                self.state.start_publishing(db, job, actor="publisher")
            if job.state == PipelineState.PUBLISHING:
                self.state.mark_published(
                    db, job,
                    external_ids=[a.external_id for a in succeeded if a.external_id],
                )
        elif status in ("queued", "in_progress"):
            # Work is still outstanding. The run stays in PUBLISHING rather than being
            # released, so nothing invites a second publish request while a command is live.
            pass
        elif job.state == PipelineState.PUBLISHING:
            # Nothing is running any more and the run is not complete. Back to
            # READY_TO_PUBLISH so the remainder can be retried - not FAILED, which would
            # describe a production that is in fact intact.
            self.state.publish_failed(db, job, reason=status)

        metadata = dict(job.metadata_json or {})
        metadata["publication_status"] = status
        metadata["publication_summary"] = {
            "total": len(attempts),
            "published": len(succeeded),
            "queued": sum(1 for a in attempts if a.status == PublishAttemptStatus.PENDING),
            "in_progress": sum(
                1 for a in attempts if a.status == PublishAttemptStatus.IN_PROGRESS
            ),
            "unresolved": sum(1 for a in attempts if a.needs_human),
            "failed": sum(
                1 for a in attempts
                if a.status in (PublishAttemptStatus.FAILED_RETRYABLE,
                                PublishAttemptStatus.FAILED_FINAL)
            ),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        job.metadata_json = metadata
        db.commit()

    @staticmethod
    def _publication_status(items: list[ItemOutcome]) -> str:
        """A summary of the items in ONE report - a dry run, or one accept request.

        Not the run's publication status: that is ``attempts_publication_status``, which
        reads every attempt from the database. This one cannot see siblings it did not
        touch, which is exactly the mistake the async runtime made it stop being used for.
        """
        if not items:
            return "none"
        done = {"published", "already_published"}
        if all(item.status in done for item in items):
            return "published"
        if any(item.status == "unknown" for item in items):
            # Surfaced above "partial" because it is the state that needs a person, and it
            # must not be hidden inside a word that sounds like ordinary progress.
            return "unresolved"
        if any(item.status == "in_progress" for item in items):
            # Another request is uploading. Reported as its own word so it is not mistaken
            # for a failure and retried by someone reading only this field.
            return "in_progress"
        if any(item.status in done for item in items):
            return "partial"
        return "failed"

    # ------------------------------------------------------------------- events

    def _emit(
        self,
        db: Session,
        job: PipelineJob,
        stage: str,
        report: PublishReport | None,
        *,
        target: PublishTarget,
        attempt: PublishAttempt | None = None,
        event_type: PipelineEventType = PipelineEventType.INFO,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "pipeline_job_id": str(job.id),
            "publish_target_id": str(target.id),
            "provider": target.platform.value,
            "publish_attempt_id": str(attempt.id) if attempt else None,
            "media_identity": attempt.media_identity if attempt else None,
            "attempt_no": attempt.attempt_no if attempt else None,
        }
        if report is not None:
            payload.update(
                {"status": report.status, "blocked_by": report.blocked_by}
            )
        payload.update(extra or {})

        event_bus.publish_event(
            db,
            service="publisher",
            event_type=event_type,
            pipeline_job_id=job.id,
            stage=stage,
            message=f"{stage} for job {job.id}",
            payload=payload,
        )

    def publisher(self):
        if self._publisher is not None:
            return self._publisher
        return YouTubePublisher(
            oauth=YouTubeOAuthClient(
                client_id=settings.youtube_client_id,
                client_secret=settings.youtube_client_secret,
                redirect_uri=settings.youtube_oauth_redirect_uri,
            ),
            timeout_sec=settings.youtube_upload_timeout_sec,
            chunk_bytes=settings.youtube_upload_chunk_mib * 1024 * 1024,
        )

    def _progress_recorder(self, attempt_id: Any):
        """A callback that commits upload progress as it happens.

        Its own short-lived session, because it is called from inside the upload while the
        outer transaction is open, and a mid-upload commit there would settle work that has
        not finished. One small UPDATE per chunk - a few dozen for a large video - is the
        price of a crash being recoverable instead of a duplicate.
        """
        from app.db.session import SessionLocal

        factory = self._session_factory or SessionLocal

        def record(session_uri: str | None, bytes_committed: int) -> None:
            values: dict[str, Any] = {"bytes_uploaded": bytes_committed}
            if session_uri:
                encrypted = _encrypt_session(session_uri)
                if encrypted:
                    values["upload_session_uri_encrypted"] = encrypted

            db = factory()
            try:
                db.execute(
                    update(PublishAttempt)
                    .where(PublishAttempt.id == attempt_id)
                    .values(**values)
                )
                db.commit()
            finally:
                db.close()

        return record

    @staticmethod
    def _resume_uri(attempt: PublishAttempt) -> str | None:
        if not attempt.upload_session_uri_encrypted:
            return None
        try:
            return secret_box.decrypt(attempt.upload_session_uri_encrypted)
        except SecretDecryptionError:
            # A session we cannot read is a session we cannot resume. Starting a new one is
            # safe here only because this path is reached for attempts that are not UNKNOWN.
            return None


# ------------------------------------------------------------------------ helpers


def idempotency_key(job_id: Any, target_id: Any, media_identity: str) -> str:
    """publish:<job>:<target>:<media>:v1

    Deterministic and derived only from what is being published and where. The ``v1`` suffix
    is the deliberate escape hatch: a genuine re-publication of the same media is requested by
    bumping it, which is an explicit act rather than a timestamp quietly making every request
    unique - the failure mode that makes idempotency keys useless.
    """
    return f"publish:{job_id}:{target_id}:{media_identity}:v1"


def _metadata_from_snapshot(snapshot: dict[str, Any], *, fallback: ResolvedMetadata):
    from app.publishing.contracts import PublishMetadata

    try:
        return PublishMetadata(
            title=str(snapshot["title"]),
            description=str(snapshot.get("description") or ""),
            tags=list(snapshot.get("tags") or []),
            privacy=str(snapshot.get("privacy") or "private"),
            category_id=snapshot.get("category_id"),
            language=snapshot.get("language"),
            made_for_kids=bool(snapshot.get("made_for_kids")),
        )
    except (KeyError, TypeError, ValueError):
        return fallback.metadata


def _encrypt_session(session_uri: str) -> str | None:
    """The session URI authorises writes to this upload; it is stored like a credential."""
    try:
        return secret_box.encrypt(session_uri)
    except Exception:  # noqa: BLE001
        # Losing resumability is acceptable; storing an upload credential in the clear is not.
        logger.warning("publish_session_uri_not_stored")
        return None


def _watch_url(external_id: str | None) -> str | None:
    return f"https://www.youtube.com/watch?v={external_id}" if external_id else None


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _log(
    job: PipelineJob,
    target: PublishTarget,
    attempt: PublishAttempt,
    result: PublishResult,
    duration_ms: int,
    status: str,
) -> None:
    """Structured, and deliberately without a field that could hold a secret.

    No access token, no refresh token, no authorization code, no session URI - the session
    URI in particular is a bearer credential that looks harmless because it is a URL.
    """
    logger.info(
        "publish_attempt",
        extra={
            "pipeline_job_id": str(job.id),
            "publish_attempt_id": str(attempt.id),
            "publish_target_id": str(target.id),
            "provider": result.provider,
            "external_id": result.external_id,
            "attempt": attempt.attempt_no,
            "status": status,
            "duration_ms": duration_ms,
            "bytes": result.bytes_uploaded,
            "retryability": attempt.retryability.value if attempt.retryability else None,
            "error_code": result.error_code,
        },
    )


def attempts_publication_status(attempts: list[PublishAttempt]) -> str:
    """What has happened to a run's publications, in one word that does not overstate.

    Ordered by what needs attention rather than by what is most common:

    * ``unresolved`` outranks everything except nothing - it is the state that needs a
      person, and burying it inside "partial" is how a duplicate video gets made later;
    * ``in_progress`` / ``queued`` are reported as themselves so nobody reads work in
      flight as work that failed and retries it;
    * ``published`` requires every attempt to have succeeded, not most of them.
    """
    if not attempts:
        return "none"
    if all(a.status == PublishAttemptStatus.SUCCEEDED for a in attempts):
        return "published"
    if any(a.needs_human for a in attempts):
        return "unresolved"
    if any(a.status == PublishAttemptStatus.IN_PROGRESS for a in attempts):
        return "in_progress"
    if any(a.status == PublishAttemptStatus.PENDING for a in attempts):
        return "queued"
    if any(a.status == PublishAttemptStatus.SUCCEEDED for a in attempts):
        return "partial"
    return "failed"


def job_publication_status(db: Session, job: PipelineJob) -> str:
    attempts = (
        db.query(PublishAttempt).filter(PublishAttempt.pipeline_job_id == job.id).all()
    )
    return attempts_publication_status(attempts)
