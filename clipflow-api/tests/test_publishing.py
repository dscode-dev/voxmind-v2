"""PR-PUBLISH-01 — the publication boundary.

No test here reaches Google. The OAuth and upload paths run against ``httpx.MockTransport``,
so the request construction and the response parsing are the real code; only the socket is
replaced.

The tests that matter most are the ones about *not* doing something: not publishing when the
gate says no, not uploading twice for one logical publication, and above all not retrying an
upload whose outcome is unknown.
"""
from __future__ import annotations

import io
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import fakeredis
import httpx
import pytest
from cryptography.fernet import Fernet

from app.core.settings import settings
from app.models.enums import (
    PipelineState,
    PublishAttemptStatus,
    PublishPlatform,
    PublishRetryability,
    PublishTargetConnectionStatus,
)
from app.models.oauth_state import OAuthState
from app.models.publish_attempt import PublishAttempt
from app.models.publish_target import PublishTarget
from app.publishing.contracts import PublishOutcome, PublishResult
from app.publishing.identity import PublisherHeartbeat
from app.publishing.publish_queue import PublishQueue
from app.publishing.media_source import MediaUnavailableError
from app.publishing.metadata import MetadataValidationError, resolve
from app.publishing.youtube_oauth import OAuthError, YouTubeOAuthClient
from app.publishing.youtube_publisher import YouTubePublisher
from app.security.secret_box import SecretBox, SecretDecryptionError
from app.services.publish_resolution_service import (
    PublishResolutionService,
    ResolutionError,
    serialize_attempt,
)
from app.services.publish_target_service import ConnectError, PublishTargetService
from app.services.publishing_service import PublishingService, idempotency_key
from tests.conftest import make_run

TEST_KEY = Fernet.generate_key().decode()
REFRESH_TOKEN = "1//super-secret-refresh-token-value"
ACCESS_TOKEN = "ya29.super-secret-access-token"


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture(autouse=True)
def publishing_config(monkeypatch):
    """A deployment that is configured and switched on. Individual tests turn things off."""
    monkeypatch.setattr(settings, "publishing_enabled", True, raising=False)
    monkeypatch.setattr(settings, "publish_secret_key", TEST_KEY, raising=False)
    monkeypatch.setattr(settings, "youtube_client_id", "client-id.apps.google", raising=False)
    monkeypatch.setattr(settings, "youtube_client_secret", "client-secret", raising=False)
    monkeypatch.setattr(
        settings, "youtube_oauth_redirect_uri", "https://clipflow.test/auth/youtube/callback",
        raising=False,
    )


@pytest.fixture()
def box():
    return SecretBox(TEST_KEY)


def oauth_client(handler) -> YouTubeOAuthClient:
    return YouTubeOAuthClient(
        client_id=settings.youtube_client_id,
        client_secret=settings.youtube_client_secret,
        redirect_uri=settings.youtube_oauth_redirect_uri,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def google_ok(request: httpx.Request) -> httpx.Response:
    """A cooperative Google: token exchange, refresh, and one channel."""
    if request.url.path.endswith("/token"):
        return httpx.Response(
            200,
            json={
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "expires_in": 3600,
                "scope": (
                    "https://www.googleapis.com/auth/youtube.upload "
                    "https://www.googleapis.com/auth/youtube.readonly"
                ),
            },
        )
    if "channels" in request.url.path:
        return httpx.Response(
            200,
            json={"items": [{"id": "UC_channel_123", "snippet": {"title": "Voxmind FC"}}]},
        )
    return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})


class StubMediaSource:
    """A final clip that exists, without MinIO."""

    def __init__(self, payload: bytes = b"x" * (1024 * 1024), missing: bool = False) -> None:
        self.payload = payload
        self.missing = missing

    def stat(self, storage_key: str) -> int:
        if self.missing:
            raise MediaUnavailableError(f"missing {storage_key}")
        return len(self.payload)

    @contextmanager
    def download(self, storage_key: str):
        if self.missing:
            raise MediaUnavailableError(f"missing {storage_key}")

        payload = self.payload

        class _Local:
            size_bytes = len(payload)
            content_type = "video/mp4"
            path = "/tmp/stub.mp4"

        yield _Local()


class StubArtifacts:
    """publish_package.json, in the shape the worker actually writes."""

    def __init__(self, videos: int = 1, generated: bool = True) -> None:
        self.package = {
            "job_id": "job-1",
            "primary_title": "Coletiva completa",
            "description": "Descricao principal",
            "hashtags": ["futebol", "coletiva"],
            "language": {"language": "pt"},
            "videos": [
                {
                    "video_index": index,
                    "post": {
                        "title": f"Titulo do video {index}",
                        "description": f"Descricao do video {index}",
                        "hashtags": ["futebol", f"clipe{index}"],
                    },
                    "final_clip": {
                        "status": "generated" if generated else "missing",
                        "file_name": f"final_clip_{index:02d}.mp4",
                    },
                }
                for index in range(1, videos + 1)
            ],
        }

    def load_json(self, storage_key):
        return self.package if storage_key.endswith("publish_package.json") else None


class StubPublisher:
    """A publisher whose outcomes the test dictates. Records every call."""

    provider = "youtube"

    def __init__(self, results=None) -> None:
        self.results = list(results or [])
        self.calls: list = []

    def publish(self, request):
        self.calls.append(request)
        if self.results:
            return self.results.pop(0)
        return PublishResult(
            provider="youtube",
            outcome=PublishOutcome.SUCCEEDED,
            external_id=f"vid_{len(self.calls)}",
            external_url=f"https://www.youtube.com/watch?v=vid_{len(self.calls)}",
            published_at=datetime.now(timezone.utc),
            privacy="private",
            bytes_uploaded=request.media.size_bytes,
        )


def make_target(db, **overrides) -> PublishTarget:
    box = SecretBox(TEST_KEY)
    fields = {
        "platform": PublishPlatform.YOUTUBE,
        "name": "Voxmind FC",
        "channel_id": "UC_channel_123",
        "channel_title": "Voxmind FC",
        "is_active": True,
        "connection_status": PublishTargetConnectionStatus.CONNECTED,
        "refresh_token_encrypted": box.encrypt(REFRESH_TOKEN),
        "config_json": {},
    }
    fields.update(overrides)
    target = PublishTarget(**fields)
    db.add(target)
    db.flush()
    return target


def make_publishable_run(db, **overrides):
    """A run that finished, passed the technical gate, and is waiting to be published."""
    fields = {
        "state": PipelineState.READY_TO_PUBLISH,
        "metadata_json": {
            "publication_eligibility": {
                "eligible": True,
                "technical_gate": "pass",
                "blocked_by": [],
            }
        },
    }
    fields.update(overrides)
    return make_run(db, **fields)


class DrainingPublishingService(PublishingService):
    """Accept a publication and then run it, in one call.

    PR-PUBLISH-QUEUE-01 moved the upload out of ``publish()`` and behind a queue. The tests
    in this module are about the publication *rules* - the QA gate, idempotency, metadata,
    what happens to an ambiguous outcome - and not about the transport, which
    ``test_publish_queue`` covers. So the harness drains the queue and reports the finished
    state, which is exactly what these tests meant when the upload was synchronous.

    The production path is untouched: this subclass adds draining and nothing else.
    """

    def publish(self, db, **kwargs):
        report = super().publish(db, **kwargs)
        if kwargs.get("dry_run", True) or report.status == "blocked":
            return report

        self._drain(db)
        return self._restate(db, report, kwargs["job"])

    def _drain(self, db, limit: int = 10) -> None:
        from app.services.publish_runtime import PublisherRuntime

        worker = PublisherRuntime(
            worker_id="test-publisher",
            queue=self.queue,
            publishing=PublishingService(
                publisher=self._publisher,
                artifacts=self.artifacts,
                media_source=self.media_source,
                targets=self.targets,
                queue=self.queue,
            ),
            heartbeat=PublisherHeartbeat("test-publisher", self.queue.redis),
            # The runtime closes the session it is handed, which is right in production -
            # every command gets its own - but here it would detach the objects the test is
            # still holding. The proxy keeps the shared session open so assertions read the
            # committed state instead of a stale copy.
            session_factory=lambda: _UnclosableSession(db),
        )
        for _ in range(limit):
            if not self.queue.depths()["ready"]:
                self.queue.promote_due_delayed()
                if not self.queue.depths()["ready"]:
                    break
            worker.tick()
        db.expire_all()

    @staticmethod
    def _restate(db, report, job):
        """Rewrite the report from the attempts, now that they have actually run."""
        from app.services.publishing_service import attempts_publication_status

        db.expire_all()
        attempts = {
            a.media_identity: a
            for a in db.query(PublishAttempt).filter(
                PublishAttempt.pipeline_job_id == job.id
            )
        }
        mapping = {
            PublishAttemptStatus.SUCCEEDED: "published",
            PublishAttemptStatus.UNKNOWN: "unknown",
            PublishAttemptStatus.NEEDS_MANUAL_RESOLUTION: "requires_manual_resolution",
            PublishAttemptStatus.FAILED_RETRYABLE: "failed",
            PublishAttemptStatus.FAILED_FINAL: "failed",
            PublishAttemptStatus.IN_PROGRESS: "in_progress",
            PublishAttemptStatus.PENDING: "queued",
            PublishAttemptStatus.CANCELED: "canceled",
        }
        for item in report.items:
            attempt = attempts.get(item.media_identity)
            if attempt is None or item.status not in ("queued", "pending_enqueue"):
                continue
            item.status = mapping.get(attempt.status, item.status)
            item.attempt_id = str(attempt.id)
            item.external_id = attempt.external_id
            item.external_url = (
                f"https://www.youtube.com/watch?v={attempt.external_id}"
                if attempt.external_id else None
            )
            item.error_code = attempt.error_code
            item.retryability = (
                attempt.retryability.value if attempt.retryability else None
            )

        report.publication_status = attempts_publication_status(list(attempts.values()))
        report.status = report.publication_status
        report.job_state = _reread_state(db, job)
        return report


class _UnclosableSession:
    """The test session, minus ``close``. Test-only scaffolding."""

    def __init__(self, session):
        self._session = session

    def close(self):
        return None

    def __getattr__(self, name):
        return getattr(self._session, name)


def _reread_state(db, job):
    from app.models.pipeline_job import PipelineJob

    fresh = db.query(PipelineJob).filter(PipelineJob.id == job.id).first()
    return fresh.state.value if fresh else job.state.value


def service(publisher=None, artifacts=None, media=None) -> PublishingService:
    return DrainingPublishingService(
        publisher=publisher or StubPublisher(),
        artifacts=artifacts or StubArtifacts(),
        media_source=media or StubMediaSource(),
        queue=PublishQueue(
            fakeredis.FakeRedis(decode_responses=True),
            "test_publish_jobs",
            worker_id="test-publisher",
        ),
    )


# ===========================================================================
# Secret storage
# ===========================================================================


def test_a_secret_survives_a_round_trip(box):
    assert box.decrypt(box.encrypt(REFRESH_TOKEN)) == REFRESH_TOKEN


def test_the_ciphertext_does_not_contain_the_plaintext(box):
    assert REFRESH_TOKEN not in box.encrypt(REFRESH_TOKEN)


def test_a_secret_cannot_be_read_with_a_different_key(box):
    other = SecretBox(Fernet.generate_key().decode())
    with pytest.raises(SecretDecryptionError):
        other.decrypt(box.encrypt(REFRESH_TOKEN))


def test_a_decryption_error_never_mentions_the_value(box):
    other = SecretBox(Fernet.generate_key().decode())
    ciphertext = box.encrypt(REFRESH_TOKEN)
    with pytest.raises(SecretDecryptionError) as caught:
        other.decrypt(ciphertext)
    message = str(caught.value)
    assert REFRESH_TOKEN not in message and ciphertext not in message


def test_without_a_key_the_box_reports_itself_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "publish_secret_key", None, raising=False)
    assert SecretBox().available is False


# ===========================================================================
# OAuth
# ===========================================================================


def test_the_authorization_url_requests_offline_access_and_minimum_scopes():
    url = oauth_client(google_ok).authorization_url(state="state-1")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "youtube.upload" in url
    assert "youtube.readonly" in url
    # The broad scopes would also grant deleting playlists, comments and captions.
    assert "auth/youtube+" not in url and "force-ssl" not in url


def test_every_state_is_unique_and_long():
    values = {YouTubeOAuthClient.new_state() for _ in range(200)}
    assert len(values) == 200
    assert all(len(value) >= 32 for value in values)


def test_begin_connect_persists_a_state_bound_to_the_admin(db):
    service_ = PublishTargetService(oauth=oauth_client(google_ok), box=SecretBox(TEST_KEY))
    result = service_.begin_connect(db, actor=None)
    assert "authorization_url" in result

    record = db.query(OAuthState).one()
    assert record.consumed_at is None
    assert record.state in result["authorization_url"]


def test_connect_refuses_to_start_without_somewhere_safe_to_put_the_token(db, monkeypatch):
    monkeypatch.setattr(settings, "publish_secret_key", None, raising=False)
    service_ = PublishTargetService(oauth=oauth_client(google_ok), box=SecretBox())
    with pytest.raises(ConnectError, match="PUBLISH_SECRET_KEY"):
        service_.begin_connect(db, actor=None)
    # Nothing was issued, so no operator is sent to a consent screen that cannot complete.
    assert db.query(OAuthState).count() == 0


def test_a_state_can_only_be_spent_once(db):
    service_ = PublishTargetService(oauth=oauth_client(google_ok), box=SecretBox(TEST_KEY))
    service_.begin_connect(db, actor=None)
    value = db.query(OAuthState).one().state

    service_.consume_state(db, value)
    with pytest.raises(ConnectError, match="already been used"):
        service_.consume_state(db, value)


def test_an_expired_state_is_refused(db):
    service_ = PublishTargetService(oauth=oauth_client(google_ok), box=SecretBox(TEST_KEY))
    db.add(
        OAuthState(
            provider="youtube",
            state="stale",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    db.flush()
    with pytest.raises(ConnectError, match="expired"):
        service_.consume_state(db, "stale")


def test_an_unknown_state_is_refused(db):
    service_ = PublishTargetService(oauth=oauth_client(google_ok), box=SecretBox(TEST_KEY))
    with pytest.raises(ConnectError, match="unknown"):
        service_.consume_state(db, "never-issued")


def test_a_completed_connect_stores_the_token_encrypted_and_resolves_the_channel(db):
    service_ = PublishTargetService(oauth=oauth_client(google_ok), box=SecretBox(TEST_KEY))
    service_.begin_connect(db, actor=None)
    state = db.query(OAuthState).one().state

    target = service_.complete_connect(db, code="auth-code", state_value=state)

    assert target.channel_id == "UC_channel_123"
    assert target.channel_title == "Voxmind FC"
    assert target.connection_status == PublishTargetConnectionStatus.CONNECTED
    # Encrypted at rest, and readable only through the box.
    assert target.refresh_token_encrypted != REFRESH_TOKEN
    assert SecretBox(TEST_KEY).decrypt(target.refresh_token_encrypted) == REFRESH_TOKEN


def test_a_freshly_connected_target_is_not_yet_enabled(db):
    service_ = PublishTargetService(oauth=oauth_client(google_ok), box=SecretBox(TEST_KEY))
    service_.begin_connect(db, actor=None)
    state = db.query(OAuthState).one().state
    target = service_.complete_connect(db, code="auth-code", state_value=state)

    assert target.is_active is False
    assert target.is_publishable is False


def test_channel_identity_is_never_taken_from_the_operator(db):
    """The channel is whatever the token reaches, not what anyone typed."""

    def other_channel(request):
        if request.url.path.endswith("/token"):
            return google_ok(request)
        return httpx.Response(
            200, json={"items": [{"id": "UC_actually_B", "snippet": {"title": "Channel B"}}]}
        )

    service_ = PublishTargetService(oauth=oauth_client(other_channel), box=SecretBox(TEST_KEY))
    service_.begin_connect(db, actor=None)
    state = db.query(OAuthState).one().state

    target = service_.complete_connect(db, code="code", state_value=state)
    assert target.channel_id == "UC_actually_B"


def test_an_exchange_without_a_refresh_token_is_refused(db):
    def no_refresh(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3600})
        return google_ok(request)

    service_ = PublishTargetService(oauth=oauth_client(no_refresh), box=SecretBox(TEST_KEY))
    service_.begin_connect(db, actor=None)
    state = db.query(OAuthState).one().state

    with pytest.raises(ConnectError, match="no_refresh_token_returned"):
        service_.complete_connect(db, code="code", state_value=state)


def test_invalid_grant_is_classified_as_unrecoverable():
    def revoked(request):
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Token has been expired"}
        )

    with pytest.raises(OAuthError) as caught:
        oauth_client(revoked).refresh_access_token(REFRESH_TOKEN)
    assert caught.value.code == "invalid_grant"
    assert caught.value.recoverable is False


def test_an_oauth_error_carries_the_code_and_not_the_description():
    def revoked(request):
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": f"secret in description {REFRESH_TOKEN}",
            },
        )

    with pytest.raises(OAuthError) as caught:
        oauth_client(revoked).refresh_access_token(REFRESH_TOKEN)
    assert REFRESH_TOKEN not in str(caught.value)


def test_a_token_bundle_does_not_print_its_tokens():
    bundle = oauth_client(google_ok).exchange_code("code")
    assert ACCESS_TOKEN not in repr(bundle)
    assert REFRESH_TOKEN not in repr(bundle)


def test_a_server_error_on_refresh_stays_recoverable():
    with pytest.raises(OAuthError) as caught:
        oauth_client(lambda r: httpx.Response(503, json={"error": "backendError"})).\
            refresh_access_token(REFRESH_TOKEN)
    assert caught.value.recoverable is True


# ===========================================================================
# Target serialization and switches
# ===========================================================================


def test_the_serialized_target_contains_no_credential(db):
    target = make_target(db)
    payload = PublishTargetService.serialize(target)
    flat = repr(payload)
    assert REFRESH_TOKEN not in flat
    assert target.refresh_token_encrypted not in flat
    assert "refresh_token" not in payload


def test_a_disabled_target_blocks_publication(db):
    job = make_publishable_run(db)
    target = make_target(db, is_active=False)

    report = service().publish(db, job=job, target=target, dry_run=True)
    assert "target_disabled" in report.blocked_by
    assert report.status == "blocked"


def test_the_global_kill_switch_blocks_publication(db, monkeypatch):
    monkeypatch.setattr(settings, "publishing_enabled", False, raising=False)
    job = make_publishable_run(db)
    target = make_target(db)

    report = service().publish(db, job=job, target=target, dry_run=False)
    assert "publishing_disabled" in report.blocked_by
    assert db.query(PublishAttempt).count() == 0


def test_a_target_needing_reconnection_blocks_publication(db):
    job = make_publishable_run(db)
    target = make_target(
        db, connection_status=PublishTargetConnectionStatus.RECONNECT_REQUIRED
    )

    report = service().publish(db, job=job, target=target, dry_run=True)
    assert "target_reconnect_required" in report.blocked_by


def test_a_target_without_a_credential_blocks_publication(db):
    job = make_publishable_run(db)
    target = make_target(db, refresh_token_encrypted=None)

    report = service().publish(db, job=job, target=target, dry_run=True)
    assert "target_no_credential" in report.blocked_by


def test_disconnecting_drops_the_credential_and_keeps_history(db):
    target = make_target(db)
    job = make_publishable_run(db)
    service().publish(db, job=job, target=target, dry_run=False)
    assert db.query(PublishAttempt).count() == 1

    PublishTargetService(oauth=oauth_client(google_ok), box=SecretBox(TEST_KEY)).disconnect(
        db, target
    )

    assert target.refresh_token_encrypted is None
    assert target.is_active is False
    assert target.connection_status == PublishTargetConnectionStatus.DISCONNECTED
    assert db.query(PublishAttempt).count() == 1


def test_a_rejected_credential_marks_the_target_for_reconnection(db):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher(
        [
            PublishResult(
                provider="youtube", outcome=PublishOutcome.FAILED,
                retryability=PublishRetryability.NOT_RETRYABLE,
                error_code="invalid_grant", error_message="oauth refresh failed",
            )
        ]
    )

    service(publisher=publisher).publish(db, job=job, target=target, dry_run=False)

    assert target.connection_status == PublishTargetConnectionStatus.RECONNECT_REQUIRED
    # A dead secret is worse than no secret.
    assert target.refresh_token_encrypted is None


# ===========================================================================
# Eligibility — the QA gate is authority
# ===========================================================================


def test_a_ready_and_eligible_run_validates(db):
    job = make_publishable_run(db)
    target = make_target(db)

    report = service().publish(db, job=job, target=target, dry_run=True)
    assert report.blocked_by == []
    assert report.status == "validated"


def test_a_run_blocked_by_technical_qa_is_never_published(db):
    """READY_TO_PUBLISH is not enough; the verdict is re-read from the run."""
    job = make_publishable_run(
        db,
        metadata_json={
            "publication_eligibility": {
                "eligible": False,
                "technical_gate": "fail",
                "blocked_by": ["final_media_qa_fail"],
            }
        },
    )
    target = make_target(db)

    report = service().publish(db, job=job, target=target, dry_run=False)
    assert "publication_not_eligible" in report.blocked_by
    assert db.query(PublishAttempt).count() == 0
    assert _reread_state(db, job) == PipelineState.READY_TO_PUBLISH.value


def test_a_run_with_no_eligibility_record_is_refused(db):
    """Fail-closed: an unmeasured gate is not a passed gate."""
    job = make_publishable_run(db, metadata_json={})
    target = make_target(db)

    report = service().publish(db, job=job, target=target, dry_run=False)
    assert "publication_eligibility_missing" in report.blocked_by
    assert db.query(PublishAttempt).count() == 0


def test_a_run_awaiting_review_is_refused(db):
    job = make_publishable_run(db, state=PipelineState.REVIEW_REQUIRED)
    target = make_target(db)

    report = service().publish(db, job=job, target=target, dry_run=False)
    assert "job_not_ready_to_publish" in report.blocked_by


def test_a_failed_run_is_refused(db):
    job = make_publishable_run(db, state=PipelineState.FAILED)
    target = make_target(db)

    report = service().publish(db, job=job, target=target, dry_run=False)
    assert "job_not_ready_to_publish" in report.blocked_by


def test_an_already_published_run_is_refused(db):
    job = make_publishable_run(db, state=PipelineState.PUBLISHED)
    target = make_target(db)

    report = service().publish(db, job=job, target=target, dry_run=False)
    assert "already_published" in report.blocked_by


def test_a_dry_run_sends_nothing(db):
    publisher = StubPublisher()
    job = make_publishable_run(db)
    target = make_target(db)

    report = service(publisher=publisher).publish(db, job=job, target=target, dry_run=True)

    assert publisher.calls == []
    assert db.query(PublishAttempt).count() == 0
    assert report.items[0].status == "would_publish"


# ===========================================================================
# Media contract
# ===========================================================================


def test_each_generated_final_clip_is_its_own_publication(db):
    job = make_publishable_run(db)
    items = service(artifacts=StubArtifacts(videos=3)).resolve_media(db, job)

    assert [item.video_index for item in items] == [1, 2, 3]
    assert items[0].storage_key == f"jobs/{job.worker_job_id}/final_clips/final_clip_01.mp4"


def test_the_final_reel_is_not_published(db):
    """It has no per-video editorial metadata; it is the review artifact."""
    job = make_publishable_run(db)
    items = service(artifacts=StubArtifacts(videos=2)).resolve_media(db, job)
    assert all("final_reel" not in item.storage_key for item in items)


def test_an_ungenerated_clip_is_skipped_not_fatal(db):
    job = make_publishable_run(db)
    artifacts = StubArtifacts(videos=2)
    artifacts.package["videos"][0]["final_clip"]["status"] = "missing"

    items = service(artifacts=artifacts).resolve_media(db, job)
    assert [item.video_index for item in items] == [2]


def test_a_selection_picks_only_the_requested_videos(db):
    job = make_publishable_run(db)
    items = service(artifacts=StubArtifacts(videos=3)).resolve_media(db, job, selection=[2])
    assert [item.video_index for item in items] == [2]


def test_missing_final_media_blocks_that_item(db):
    job = make_publishable_run(db)
    target = make_target(db)

    report = service(media=StubMediaSource(missing=True)).publish(
        db, job=job, target=target, dry_run=False
    )
    assert report.items[0].blocked_by == ["final_media_unavailable"]
    # No attempt was created, so there is nothing to report a publication status for -
    # "none" rather than "failed", which would imply something was tried.
    assert report.publication_status == "none"
    assert db.query(PublishAttempt).count() == 0


# ===========================================================================
# Metadata
# ===========================================================================


def test_precedence_runs_request_then_package_then_target_then_system():
    resolved = resolve(
        video={"post": {"title": "from package"}},
        package={},
        target_config={"default_title": "from target", "default_privacy": "unlisted"},
        overrides={"title": "from request"},
    )
    assert resolved.metadata.title == "from request"
    assert resolved.sources["title"] == "request"
    # Privacy is not editorial, so the package never supplies it.
    assert resolved.metadata.privacy == "unlisted"
    assert resolved.sources["privacy"] == "target_default"


def test_privacy_defaults_to_private():
    resolved = resolve(video={"post": {"title": "t"}}, package={}, target_config={},
                       overrides={})
    assert resolved.metadata.privacy == "private"
    assert resolved.sources["privacy"] == "system_default"


def test_an_over_long_title_blocks_rather_than_being_truncated():
    with pytest.raises(MetadataValidationError, match="title_too_long"):
        resolve(video={"post": {"title": "a" * 101}}, package={}, target_config={},
                overrides={})


def test_a_missing_title_blocks():
    with pytest.raises(MetadataValidationError, match="title_missing"):
        resolve(video={}, package={}, target_config={}, overrides={})


def test_an_over_long_description_is_truncated_and_the_fact_is_recorded():
    resolved = resolve(
        video={"post": {"title": "t", "description": "word " * 2000}},
        package={}, target_config={}, overrides={},
    )
    assert len(resolved.metadata.description) <= 5000
    assert "description_truncated_to_api_limit" in resolved.notes


def test_hashtags_are_preserved_in_the_description():
    resolved = resolve(
        video={"post": {"title": "t", "description": "corpo", "hashtags": ["futebol"]}},
        package={}, target_config={}, overrides={},
    )
    assert "#futebol" in resolved.metadata.description


def test_tags_are_capped_at_the_api_budget():
    resolved = resolve(
        video={"post": {"title": "t", "hashtags": [f"tag{i:02d}" for i in range(200)]}},
        package={}, target_config={}, overrides={},
    )
    assert sum(len(tag) for tag in resolved.metadata.tags) <= 500


def test_a_non_numeric_category_is_refused():
    with pytest.raises(MetadataValidationError, match="category_id_invalid"):
        resolve(video={"post": {"title": "t"}}, package={},
                target_config={"default_category_id": "Sports"}, overrides={})


def test_no_category_is_invented():
    resolved = resolve(video={"post": {"title": "t"}}, package={}, target_config={},
                       overrides={})
    assert resolved.metadata.category_id is None


def test_made_for_kids_is_configured_never_inferred():
    resolved = resolve(video={"post": {"title": "t"}}, package={},
                       target_config={"made_for_kids": True}, overrides={})
    assert resolved.metadata.made_for_kids is True
    assert resolved.sources["made_for_kids"] == "target_default"


def test_angle_brackets_are_stripped_because_the_api_rejects_them():
    resolved = resolve(video={"post": {"title": "A <b> B"}}, package={}, target_config={},
                       overrides={})
    assert "<" not in resolved.metadata.title


def test_invalid_metadata_blocks_the_item_without_creating_an_attempt(db):
    job = make_publishable_run(db)
    target = make_target(db)
    artifacts = StubArtifacts()
    artifacts.package["videos"][0]["post"]["title"] = "z" * 400

    report = service(artifacts=artifacts).publish(db, job=job, target=target, dry_run=False)

    assert report.items[0].blocked_by == ["metadata_invalid"]
    assert db.query(PublishAttempt).count() == 0


# ===========================================================================
# Idempotency
# ===========================================================================


def test_the_key_is_deterministic_and_carries_no_timestamp():
    job_id, target_id = uuid.uuid4(), uuid.uuid4()
    first = idempotency_key(job_id, target_id, "final_clips/a.mp4")
    second = idempotency_key(job_id, target_id, "final_clips/a.mp4")
    assert first == second
    assert first.endswith(":v1")


def test_publishing_the_same_media_twice_produces_one_attempt_and_one_upload(db):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher()
    svc = service(publisher=publisher)

    first = svc.publish(db, job=job, target=target, dry_run=False)
    second = svc.publish(db, job=job, target=target, dry_run=False)

    assert db.query(PublishAttempt).count() == 1
    assert len(publisher.calls) == 1, "the second request must not re-upload"
    assert first.items[0].status == "published"
    # The run itself is now PUBLISHED, so the second request is refused before it reaches
    # the item at all - the outer guard fires first, which is the stricter of the two.
    assert second.blocked_by == ["already_published"]


def test_an_item_already_published_is_not_re_uploaded_while_the_run_continues(db):
    """The per-item guard, isolated from the run-level one.

    Two outputs: the first has already succeeded, so only the second is uploaded and the
    run is not yet PUBLISHED when the request arrives.
    """
    job = make_publishable_run(db)
    target = make_target(db)
    db.add(
        PublishAttempt(
            pipeline_job_id=job.id, target_id=target.id,
            idempotency_key=idempotency_key(job.id, target.id,
                                            "final_clips/final_clip_01.mp4"),
            media_identity="final_clips/final_clip_01.mp4",
            status=PublishAttemptStatus.SUCCEEDED, external_id="vid_earlier", attempt_no=1,
        )
    )
    db.flush()

    publisher = StubPublisher()
    report = service(publisher=publisher, artifacts=StubArtifacts(videos=2)).publish(
        db, job=job, target=target, dry_run=False
    )

    assert [item.status for item in report.items] == ["already_published", "published"]
    assert report.items[0].external_id == "vid_earlier"
    assert len(publisher.calls) == 1, "only the outstanding item is uploaded"
    assert db.query(PublishAttempt).count() == 2


def test_a_concurrent_duplicate_resolves_to_the_same_attempt(db):
    """The unique index settles the race, not the SELECT that precedes it."""
    job = make_publishable_run(db)
    target = make_target(db)
    key = idempotency_key(job.id, target.id, "final_clips/final_clip_01.mp4")

    # The row the "other request" committed first.
    winner = PublishAttempt(
        pipeline_job_id=job.id, target_id=target.id, idempotency_key=key,
        media_identity="final_clips/final_clip_01.mp4",
        status=PublishAttemptStatus.SUCCEEDED, external_id="vid_winner", attempt_no=1,
    )
    db.add(winner)
    db.flush()

    publisher = StubPublisher()
    report = service(publisher=publisher).publish(db, job=job, target=target, dry_run=False)

    assert db.query(PublishAttempt).count() == 1
    assert publisher.calls == []
    assert report.items[0].external_id == "vid_winner"


def test_a_retry_reuses_the_row_and_does_not_create_a_second_publication(db):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher(
        [
            PublishResult(
                provider="youtube", outcome=PublishOutcome.FAILED,
                retryability=PublishRetryability.RETRYABLE,
                error_code="backendError", error_message="provider returned 503",
            )
        ]
    )
    svc = service(publisher=publisher)

    first = svc.publish(db, job=job, target=target, dry_run=False)
    assert first.items[0].status == "failed"
    assert db.query(PublishAttempt).one().status == PublishAttemptStatus.FAILED_RETRYABLE

    second = svc.publish(db, job=job, target=target, dry_run=False)

    assert second.items[0].status == "published"
    attempt = db.query(PublishAttempt).one()
    assert attempt.attempt_no == 2, "one row, two attempts"
    assert db.query(PublishAttempt).count() == 1


def test_an_upload_can_only_be_claimed_once(db):
    """The unique index deduplicates the row; the claim deduplicates the upload.

    Regression: the concurrency smoke found that a request which lost the insert race read
    the winner's row and uploaded the same media a second time - one attempt row, two videos.
    """
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = PublishAttempt(
        pipeline_job_id=job.id, target_id=target.id,
        idempotency_key=idempotency_key(job.id, target.id, "final_clips/final_clip_01.mp4"),
        media_identity="final_clips/final_clip_01.mp4",
        status=PublishAttemptStatus.PENDING, attempt_no=0,
    )
    db.add(attempt)
    db.flush()

    svc = service()
    assert svc._claim(db, attempt) is True
    assert attempt.status == PublishAttemptStatus.IN_PROGRESS
    assert attempt.attempt_no == 1
    # A second claimer finds the row already taken and must not upload.
    assert svc._claim(db, attempt) is False
    assert attempt.attempt_no == 1, "a refused claim must not consume an attempt"


def test_a_request_that_loses_the_claim_uploads_nothing(db):
    job = make_publishable_run(db)
    target = make_target(db)
    # Left IN_PROGRESS as another process would leave it mid-upload.
    db.add(
        PublishAttempt(
            pipeline_job_id=job.id, target_id=target.id,
            idempotency_key=idempotency_key(job.id, target.id,
                                            "final_clips/final_clip_01.mp4"),
            media_identity="final_clips/final_clip_01.mp4",
            status=PublishAttemptStatus.IN_PROGRESS, attempt_no=1,
        )
    )
    db.flush()

    publisher = StubPublisher()
    report = service(publisher=publisher).publish(db, job=job, target=target, dry_run=False)

    assert publisher.calls == [], "an in-flight upload must not be duplicated"
    assert report.items[0].status == "in_progress"
    assert report.publication_status == "in_progress"


def test_an_in_progress_attempt_is_not_reported_as_a_failure(db):
    """It is neither done nor broken; calling it failed invites a retry that duplicates."""
    job = make_publishable_run(db)
    target = make_target(db)
    db.add(
        PublishAttempt(
            pipeline_job_id=job.id, target_id=target.id,
            idempotency_key=idempotency_key(job.id, target.id,
                                            "final_clips/final_clip_01.mp4"),
            media_identity="final_clips/final_clip_01.mp4",
            status=PublishAttemptStatus.IN_PROGRESS, attempt_no=1,
        )
    )
    db.flush()

    report = service().publish(db, job=job, target=target, dry_run=False)
    assert report.publication_status != "failed"
    assert _reread_state(db, job) != PipelineState.PUBLISHED.value


def test_a_final_failure_is_not_retried(db):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher(
        [
            PublishResult(
                provider="youtube", outcome=PublishOutcome.FAILED,
                retryability=PublishRetryability.NOT_RETRYABLE,
                error_code="invalidTitle", error_message="provider returned 400",
            )
        ]
    )
    svc = service(publisher=publisher)

    svc.publish(db, job=job, target=target, dry_run=False)
    second = svc.publish(db, job=job, target=target, dry_run=False)

    assert len(publisher.calls) == 1
    assert second.items[0].blocked_by == ["attempt_failed_final"]


# ===========================================================================
# UNKNOWN — the invariant this PR exists for
# ===========================================================================


def _unknown_result() -> PublishResult:
    return PublishResult(
        provider="youtube", outcome=PublishOutcome.UNKNOWN,
        error_code="ReadTimeout",
        error_message="connection lost after sending the final chunk; the video may exist",
        bytes_uploaded=1024,
        session_uri="https://upload.googleapis.com/session/abc",
    )


def test_an_ambiguous_ending_is_recorded_as_unknown(db):
    job = make_publishable_run(db)
    target = make_target(db)

    report = service(publisher=StubPublisher([_unknown_result()])).publish(
        db, job=job, target=target, dry_run=False
    )

    attempt = db.query(PublishAttempt).one()
    assert attempt.status == PublishAttemptStatus.UNKNOWN
    assert attempt.retryability == PublishRetryability.REQUIRES_MANUAL_RESOLUTION
    assert report.items[0].status == "unknown"
    assert report.publication_status == "unresolved"


def test_an_unknown_attempt_is_never_uploaded_again(db):
    """THE invariant: a second request must not risk a duplicate public video."""
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher([_unknown_result()])
    svc = service(publisher=publisher)

    svc.publish(db, job=job, target=target, dry_run=False)
    assert len(publisher.calls) == 1

    second = svc.publish(db, job=job, target=target, dry_run=False)

    assert len(publisher.calls) == 1, "no blind retry after an ambiguous outcome"
    assert second.items[0].status == "requires_manual_resolution"
    assert second.items[0].blocked_by == ["attempt_requires_manual_resolution"]


def test_an_unknown_outcome_does_not_publish_the_run(db):
    job = make_publishable_run(db)
    target = make_target(db)

    service(publisher=StubPublisher([_unknown_result()])).publish(
        db, job=job, target=target, dry_run=False
    )
    assert _reread_state(db, job) != PipelineState.PUBLISHED.value


def test_an_unknown_attempt_keeps_its_session_encrypted(db):
    job = make_publishable_run(db)
    target = make_target(db)

    service(publisher=StubPublisher([_unknown_result()])).publish(
        db, job=job, target=target, dry_run=False
    )

    attempt = db.query(PublishAttempt).one()
    assert attempt.upload_session_uri_encrypted is not None
    assert "upload.googleapis.com" not in attempt.upload_session_uri_encrypted


def test_the_session_uri_is_absent_from_the_api_view(db):
    job = make_publishable_run(db)
    target = make_target(db)
    service(publisher=StubPublisher([_unknown_result()])).publish(
        db, job=job, target=target, dry_run=False
    )

    payload = serialize_attempt(db.query(PublishAttempt).one())
    flat = repr(payload)
    assert "upload.googleapis.com" not in flat
    assert "upload_session_uri" not in payload
    assert payload["has_resumable_session"] is True


# ===========================================================================
# Resolution
# ===========================================================================


def _unknown_attempt(db, job, target) -> PublishAttempt:
    service(publisher=StubPublisher([_unknown_result()])).publish(
        db, job=job, target=target, dry_run=False
    )
    return db.query(PublishAttempt).one()


def test_reconcile_settles_an_unknown_when_the_session_reports_completion(db):
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _unknown_attempt(db, job, target)

    def session_completed(request):
        return httpx.Response(200, json={"id": "vid_from_session",
                                         "status": {"privacyStatus": "private"}})

    result = PublishResolutionService(
        client=httpx.Client(transport=httpx.MockTransport(session_completed))
    ).reconcile(db, attempt)

    assert result["external_id"] == "vid_from_session"
    assert attempt.status == PublishAttemptStatus.SUCCEEDED
    assert attempt.external_id_source == "reconciled"
    assert _reread_state(db, job) == PipelineState.PUBLISHED.value


def test_reconcile_refuses_to_guess_when_the_session_cannot_answer(db):
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _unknown_attempt(db, job, target)

    def expired(request):
        return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})

    result = PublishResolutionService(
        client=httpx.Client(transport=httpx.MockTransport(expired))
    ).reconcile(db, attempt)

    assert attempt.status == PublishAttemptStatus.NEEDS_MANUAL_RESOLUTION
    assert result["external_id"] is None
    assert _reread_state(db, job) != PipelineState.PUBLISHED.value


def test_reconcile_reports_incomplete_when_the_session_is_still_open(db):
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _unknown_attempt(db, job, target)

    def incomplete(request):
        return httpx.Response(308, headers={"Range": "bytes=0-511"})

    PublishResolutionService(
        client=httpx.Client(transport=httpx.MockTransport(incomplete))
    ).reconcile(db, attempt)

    assert attempt.status == PublishAttemptStatus.NEEDS_MANUAL_RESOLUTION
    assert attempt.error_code == "session_inconclusive"


def test_only_an_unknown_attempt_can_be_reconciled(db):
    job = make_publishable_run(db)
    target = make_target(db)
    service().publish(db, job=job, target=target, dry_run=False)
    attempt = db.query(PublishAttempt).one()

    with pytest.raises(ResolutionError, match="only an unknown attempt"):
        PublishResolutionService().reconcile(db, attempt)


def test_an_operator_can_resolve_with_a_verified_video_id(db):
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _unknown_attempt(db, job, target)

    def verify(request):
        if request.url.path.endswith("/token"):
            return google_ok(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "vid_operator",
                        "snippet": {"channelId": "UC_channel_123"},
                        "status": {"privacyStatus": "private"},
                    }
                ]
            },
        )

    result = PublishResolutionService(
        client=httpx.Client(transport=httpx.MockTransport(verify))
    ).resolve(db, attempt, external_id="vid_operator")

    assert result["external_id"] == "vid_operator"
    assert attempt.external_id_source == "operator"
    assert attempt.provider_metadata_json["verified"] is True
    assert _reread_state(db, job) == PipelineState.PUBLISHED.value


def test_a_video_id_that_does_not_exist_is_refused(db):
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _unknown_attempt(db, job, target)

    def empty(request):
        if request.url.path.endswith("/token"):
            return google_ok(request)
        return httpx.Response(200, json={"items": []})

    with pytest.raises(ResolutionError, match="not found"):
        PublishResolutionService(
            client=httpx.Client(transport=httpx.MockTransport(empty))
        ).resolve(db, attempt, external_id="vid_typo")

    assert attempt.status == PublishAttemptStatus.UNKNOWN


def test_a_video_belonging_to_another_channel_is_refused(db):
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _unknown_attempt(db, job, target)

    def other_channel(request):
        if request.url.path.endswith("/token"):
            return google_ok(request)
        return httpx.Response(
            200,
            json={"items": [{"id": "vid_x", "snippet": {"channelId": "UC_someone_else"}}]},
        )

    with pytest.raises(ResolutionError, match="belongs to channel"):
        PublishResolutionService(
            client=httpx.Client(transport=httpx.MockTransport(other_channel))
        ).resolve(db, attempt, external_id="vid_x")


def test_an_operator_can_record_that_nothing_was_published(db):
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _unknown_attempt(db, job, target)

    PublishResolutionService().mark_not_published(db, attempt, note="checked the channel")

    assert attempt.status == PublishAttemptStatus.FAILED_FINAL
    assert attempt.external_id is None
    assert _reread_state(db, job) == PipelineState.READY_TO_PUBLISH.value


def test_a_started_attempt_cannot_be_cancelled(db):
    """Cancelling would imply the video was removed; nothing here removes anything."""
    job = make_publishable_run(db)
    target = make_target(db)
    service().publish(db, job=job, target=target, dry_run=False)
    attempt = db.query(PublishAttempt).one()

    with pytest.raises(ResolutionError, match="only a pending attempt"):
        PublishResolutionService().cancel(db, attempt)


# ===========================================================================
# Multiple outputs
# ===========================================================================


def test_all_outputs_succeeding_publishes_the_run(db):
    job = make_publishable_run(db)
    target = make_target(db)

    report = service(artifacts=StubArtifacts(videos=3)).publish(
        db, job=job, target=target, dry_run=False
    )

    assert report.publication_status == "published"
    assert db.query(PublishAttempt).count() == 3
    assert _reread_state(db, job) == PipelineState.PUBLISHED.value


def test_a_partial_result_does_not_publish_the_run(db):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher(
        [
            PublishResult(provider="youtube", outcome=PublishOutcome.SUCCEEDED,
                          external_id="vid_1", bytes_uploaded=10),
            PublishResult(provider="youtube", outcome=PublishOutcome.FAILED,
                          retryability=PublishRetryability.RETRYABLE,
                          error_code="backendError", error_message="503"),
        ]
    )

    report = service(publisher=publisher, artifacts=StubArtifacts(videos=2)).publish(
        db, job=job, target=target, dry_run=False
    )

    assert report.publication_status == "partial"
    assert _reread_state(db, job) == PipelineState.READY_TO_PUBLISH.value
    # The success is kept, not rolled back.
    assert db.query(PublishAttempt).filter(
        PublishAttempt.status == PublishAttemptStatus.SUCCEEDED
    ).count() == 1
    assert (job.metadata_json or {}).get("publication_status") == "partial"


def test_one_unknown_among_successes_surfaces_as_unresolved(db):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher(
        [
            PublishResult(provider="youtube", outcome=PublishOutcome.SUCCEEDED,
                          external_id="vid_1", bytes_uploaded=10),
            _unknown_result(),
        ]
    )

    report = service(publisher=publisher, artifacts=StubArtifacts(videos=2)).publish(
        db, job=job, target=target, dry_run=False
    )

    # "unresolved" rather than "partial": this one needs a person, and hiding it inside a
    # word that sounds like ordinary progress is how a duplicate video happens later.
    assert report.publication_status == "unresolved"
    assert _reread_state(db, job) != PipelineState.PUBLISHED.value


def test_resolving_the_last_outstanding_item_publishes_the_run(db):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher(
        [
            PublishResult(provider="youtube", outcome=PublishOutcome.SUCCEEDED,
                          external_id="vid_1", bytes_uploaded=10),
            _unknown_result(),
        ]
    )
    service(publisher=publisher, artifacts=StubArtifacts(videos=2)).publish(
        db, job=job, target=target, dry_run=False
    )
    assert _reread_state(db, job) != PipelineState.PUBLISHED.value

    unknown = db.query(PublishAttempt).filter(
        PublishAttempt.status == PublishAttemptStatus.UNKNOWN
    ).one()

    def session_completed(request):
        return httpx.Response(200, json={"id": "vid_2"})

    PublishResolutionService(
        client=httpx.Client(transport=httpx.MockTransport(session_completed))
    ).reconcile(db, unknown)

    assert _reread_state(db, job) == PipelineState.PUBLISHED.value


# ===========================================================================
# Metadata snapshot
# ===========================================================================


def test_the_snapshot_is_frozen_when_the_attempt_is_created(db):
    job = make_publishable_run(db)
    target = make_target(db)
    artifacts = StubArtifacts()
    publisher = StubPublisher(
        [
            PublishResult(provider="youtube", outcome=PublishOutcome.FAILED,
                          retryability=PublishRetryability.RETRYABLE,
                          error_code="backendError", error_message="503"),
        ]
    )
    svc = service(publisher=publisher, artifacts=artifacts)

    svc.publish(db, job=job, target=target, dry_run=False)
    original = db.query(PublishAttempt).one().payload_json["metadata"]["title"]

    # The package is rewritten between the two tries, as a re-render would do.
    artifacts.package["videos"][0]["post"]["title"] = "Titulo completamente diferente"
    svc.publish(db, job=job, target=target, dry_run=False)

    attempt = db.query(PublishAttempt).one()
    assert attempt.payload_json["metadata"]["title"] == original
    # And what was actually sent on the retry is the frozen value, not the new one.
    assert publisher.calls[-1].metadata.title == original


def test_the_snapshot_records_where_each_field_came_from(db):
    job = make_publishable_run(db)
    target = make_target(db, config_json={"default_privacy": "unlisted"})
    service().publish(db, job=job, target=target, dry_run=False)

    snapshot = db.query(PublishAttempt).one().payload_json["metadata"]
    assert snapshot["sources"]["title"] == "publish_package"
    assert snapshot["sources"]["privacy"] == "target_default"
    assert snapshot["privacy"] == "unlisted"


def test_the_snapshot_carries_a_contract_version(db):
    job = make_publishable_run(db)
    target = make_target(db)
    service().publish(db, job=job, target=target, dry_run=False)
    assert db.query(PublishAttempt).one().payload_json["publish_contract_version"]


# ===========================================================================
# State machine
# ===========================================================================


def test_published_is_only_reachable_through_publishing(db):
    from app.services.pipeline_state_machine import can_transition

    assert can_transition(PipelineState.READY_TO_PUBLISH, PipelineState.PUBLISHING)
    assert can_transition(PipelineState.PUBLISHING, PipelineState.PUBLISHED)
    assert not can_transition(PipelineState.READY_TO_PUBLISH, PipelineState.PUBLISHED)
    assert not can_transition(PipelineState.REVIEW_REQUIRED, PipelineState.PUBLISHING)


def test_a_failed_publish_releases_the_run_rather_than_failing_it(db):
    """The render is intact; only the upload did not land."""
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher(
        [
            PublishResult(provider="youtube", outcome=PublishOutcome.FAILED,
                          retryability=PublishRetryability.RETRYABLE,
                          error_code="backendError", error_message="503"),
        ]
    )

    report = service(publisher=publisher).publish(
        db, job=job, target=target, dry_run=False
    )

    assert report.job_state == PipelineState.READY_TO_PUBLISH.value
    assert report.job_state != PipelineState.FAILED.value


def test_the_published_transition_records_the_external_ids(db, no_event_fanout):
    from app.models.pipeline_event import PipelineEvent

    job = make_publishable_run(db)
    target = make_target(db)
    service().publish(db, job=job, target=target, dry_run=False)

    event = (
        db.query(PipelineEvent)
        .filter(PipelineEvent.stage == "published")
        .order_by(PipelineEvent.created_at.desc())
        .first()
    )
    assert event is not None
    assert event.payload_json["external_ids"]


# ===========================================================================
# Provider adapter — classification
# ===========================================================================


def _publisher(handler, chunk_bytes: int = 256 * 1024) -> YouTubePublisher:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return YouTubePublisher(oauth=oauth_client(google_ok), client=client,
                            chunk_bytes=chunk_bytes)


def _request(size: int = 512 * 1024):
    from app.publishing.contracts import (
        PublishCredential,
        PublishMedia,
        PublishMetadata,
        PublishRequest,
    )

    payload = b"v" * size
    return PublishRequest(
        attempt_id="a", pipeline_job_id="j", publish_target_id="t",
        media=PublishMedia(
            identity="final_clips/a.mp4", storage_key="k", size_bytes=size,
            content_type="video/mp4", open_stream=lambda: io.BytesIO(payload),
        ),
        metadata=PublishMetadata(
            title="t", description="d", tags=["a"], privacy="private",
            category_id=None, language="pt", made_for_kids=False,
        ),
        credential=PublishCredential(refresh_token=REFRESH_TOKEN, client_id="c",
                                     client_secret="s"),
    )


def _session_then(chunk_response):
    """A handler that opens a session, then answers every chunk with ``chunk_response``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return google_ok(request)
        if request.method == "POST":
            return httpx.Response(
                200, headers={"Location": "https://upload.googleapis.com/session/xyz"}
            )
        return chunk_response(request)

    return handler


def test_a_successful_upload_returns_the_video_id():
    def chunks(request):
        content_range = request.headers.get("Content-Range", "")
        if content_range.endswith("/524288") and "-524287" in content_range:
            return httpx.Response(200, json={"id": "vid_ok",
                                             "status": {"privacyStatus": "private",
                                                        "uploadStatus": "uploaded"}})
        end = int(content_range.split("-")[1].split("/")[0])
        return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})

    result = _publisher(_session_then(chunks)).publish(_request())

    assert result.succeeded
    assert result.external_id == "vid_ok"
    assert result.external_url == "https://www.youtube.com/watch?v=vid_ok"
    assert result.bytes_uploaded == 512 * 1024


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        (401, "authError", PublishRetryability.NOT_RETRYABLE),
        (403, "forbidden", PublishRetryability.NOT_RETRYABLE),
        (403, "quotaExceeded", PublishRetryability.RETRYABLE),
        (400, "invalidTitle", PublishRetryability.NOT_RETRYABLE),
        (429, "rateLimitExceeded", PublishRetryability.RETRYABLE),
        (500, "backendError", PublishRetryability.RETRYABLE),
        (503, "backendError", PublishRetryability.RETRYABLE),
    ],
)
def test_provider_failures_are_classified(status, reason, expected):
    """Status code alone is not enough: a 403 is quota (later) or forbidden (never)."""

    def handler(request):
        if request.url.path.endswith("/token"):
            return google_ok(request)
        return httpx.Response(status, json={"error": {"errors": [{"reason": reason}]}})

    result = _publisher(handler).publish(_request())

    assert result.outcome == PublishOutcome.FAILED
    assert result.retryability is expected
    assert result.error_code == reason


def test_a_timeout_opening_the_session_is_retryable_not_unknown():
    """Nothing was created, so there is no video to duplicate."""

    def handler(request):
        if request.url.path.endswith("/token"):
            return google_ok(request)
        raise httpx.ReadTimeout("timed out", request=request)

    result = _publisher(handler).publish(_request())

    assert result.outcome == PublishOutcome.FAILED
    assert result.retryability is PublishRetryability.RETRYABLE
    assert result.bytes_uploaded == 0


def test_a_timeout_on_a_middle_chunk_is_retryable():
    state = {"chunk": 0}

    def chunks(request):
        state["chunk"] += 1
        if state["chunk"] == 1:
            return httpx.Response(308, headers={"Range": "bytes=0-262143"})
        raise httpx.ReadTimeout("dropped", request=request)

    # 1 MiB in 256 KiB chunks: the failure lands on chunk 2 of 4.
    result = _publisher(_session_then(chunks)).publish(_request(size=1024 * 1024))

    assert result.outcome == PublishOutcome.FAILED
    assert result.retryability is PublishRetryability.RETRYABLE
    assert result.session_uri, "the session is kept so the upload can resume"


def test_a_timeout_on_the_final_chunk_is_unknown():
    """The bytes may all have landed. This is the case that must never auto-retry."""

    def chunks(request):
        content_range = request.headers.get("Content-Range", "")
        end = int(content_range.split("-")[1].split("/")[0])
        if end >= 512 * 1024 - 1:
            raise httpx.ReadTimeout("lost after last byte", request=request)
        return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})

    result = _publisher(_session_then(chunks)).publish(_request())

    assert result.outcome == PublishOutcome.UNKNOWN
    assert result.retryability is None, "unknown must not carry a retry verdict"


def test_a_server_error_on_the_final_chunk_is_unknown():
    def chunks(request):
        content_range = request.headers.get("Content-Range", "")
        end = int(content_range.split("-")[1].split("/")[0])
        if end >= 512 * 1024 - 1:
            return httpx.Response(500, json={"error": {"errors": [{"reason": "backendError"}]}})
        return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})

    result = _publisher(_session_then(chunks)).publish(_request())
    assert result.outcome == PublishOutcome.UNKNOWN


def test_a_success_without_a_video_id_is_unknown_not_success():
    def chunks(request):
        content_range = request.headers.get("Content-Range", "")
        end = int(content_range.split("-")[1].split("/")[0])
        if end >= 512 * 1024 - 1:
            return httpx.Response(200, json={"status": {"uploadStatus": "uploaded"}})
        return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})

    result = _publisher(_session_then(chunks)).publish(_request())

    assert result.outcome == PublishOutcome.UNKNOWN
    assert result.error_code == "missing_video_id"


def test_a_resumed_session_that_already_finished_is_a_success():
    """The happy resolution of a previous ambiguous ending."""

    def handler(request):
        if request.url.path.endswith("/token"):
            return google_ok(request)
        if request.headers.get("Content-Range", "").startswith("bytes */"):
            return httpx.Response(200, json={"id": "vid_was_there_all_along"})
        return httpx.Response(500)

    request = _request()
    resumed = type(request)(
        attempt_id=request.attempt_id, pipeline_job_id=request.pipeline_job_id,
        publish_target_id=request.publish_target_id, media=request.media,
        metadata=request.metadata, credential=request.credential,
        resume_session_uri="https://upload.googleapis.com/session/xyz",
    )

    result = _publisher(handler).publish(resumed)

    assert result.succeeded
    assert result.external_id == "vid_was_there_all_along"


def test_the_upload_streams_rather_than_buffering():
    """Every chunk sent is bounded by the configured chunk size, not the file size."""
    seen: list[int] = []

    def chunks(request):
        seen.append(len(request.content))
        content_range = request.headers.get("Content-Range", "")
        end = int(content_range.split("-")[1].split("/")[0])
        if end >= 4 * 1024 * 1024 - 1:
            return httpx.Response(200, json={"id": "vid_stream"})
        return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})

    result = _publisher(_session_then(chunks), chunk_bytes=256 * 1024).publish(
        _request(size=4 * 1024 * 1024)
    )

    assert result.succeeded
    assert max(seen) <= 256 * 1024
    assert len(seen) == 16


def test_the_video_resource_declares_made_for_kids_and_privacy():
    captured: dict = {}

    def handler(request):
        if request.url.path.endswith("/token"):
            return google_ok(request)
        if request.method == "POST":
            import json

            captured.update(json.loads(request.content))
            return httpx.Response(
                200, headers={"Location": "https://upload.googleapis.com/session/xyz"}
            )
        content_range = request.headers.get("Content-Range", "")
        end = int(content_range.split("-")[1].split("/")[0])
        if end >= 512 * 1024 - 1:
            return httpx.Response(200, json={"id": "vid"})
        return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})

    _publisher(handler).publish(_request())

    assert captured["status"]["privacyStatus"] == "private"
    assert captured["status"]["selfDeclaredMadeForKids"] is False
    assert captured["snippet"]["defaultLanguage"] == "pt"


# ===========================================================================
# Secrets never leak
# ===========================================================================


def test_no_token_appears_in_the_attempt_row(db):
    job = make_publishable_run(db)
    target = make_target(db)
    service().publish(db, job=job, target=target, dry_run=False)

    attempt = db.query(PublishAttempt).one()
    flat = repr(
        {
            "payload": attempt.payload_json,
            "provider": attempt.provider_metadata_json,
            "error": attempt.error_message,
        }
    )
    assert REFRESH_TOKEN not in flat
    assert ACCESS_TOKEN not in flat


def test_no_token_appears_in_the_logs(db, caplog):
    job = make_publishable_run(db)
    target = make_target(db)

    with caplog.at_level(logging.DEBUG):
        service().publish(db, job=job, target=target, dry_run=False)

    text = "\n".join(record.getMessage() + repr(record.__dict__) for record in caplog.records)
    assert REFRESH_TOKEN not in text
    assert ACCESS_TOKEN not in text
    assert "upload.googleapis.com/session" not in text


def test_a_provider_error_echoing_a_credential_is_not_persisted(db):
    """Google echoes request parameters into some errors; only the code is kept."""
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher(
        [
            PublishResult(
                provider="youtube", outcome=PublishOutcome.FAILED,
                retryability=PublishRetryability.NOT_RETRYABLE,
                error_code="invalid_grant", error_message="oauth refresh failed",
            )
        ]
    )

    service(publisher=publisher).publish(db, job=job, target=target, dry_run=False)

    attempt = db.query(PublishAttempt).one()
    assert attempt.error_code == "invalid_grant"
    assert REFRESH_TOKEN not in (attempt.error_message or "")


def test_the_provider_reason_parser_drops_the_description():
    from app.publishing.youtube_publisher import provider_reason

    response = httpx.Response(
        403,
        json={
            "error": {
                "errors": [{"reason": "quotaExceeded", "message": f"token {ACCESS_TOKEN}"}],
                "message": f"token {ACCESS_TOKEN}",
            }
        },
    )
    assert provider_reason(response) == "quotaExceeded"


# ===========================================================================
# Nothing publishes on its own
# ===========================================================================


def test_the_scheduler_does_not_reach_the_publisher():
    """PR-SCHEDULER-01's loop must remain discovery -> selection -> admission.

    Checks imports and names, not prose. Those modules legitimately talk about publishing in
    their docstrings and call ``event_bus.publish_event``, so a substring match on the source
    flags the very comments that document the boundary.
    """
    import ast
    import inspect

    from app.services import automation_scheduler, automation_service

    forbidden_names = {
        "PublishingService",
        "PublishResolutionService",
        "PublishTargetService",
        "YouTubePublisher",
        "YouTubeOAuthClient",
    }

    for module in (automation_service, automation_scheduler):
        tree = ast.parse(inspect.getsource(module))

        modules_imported: set[str] = set()
        names_imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules_imported.add(node.module)
                names_imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                modules_imported.update(alias.name for alias in node.names)

        offending_modules = {
            name
            for name in modules_imported
            if name.startswith("app.publishing") or "publish" in name.rsplit(".", 1)[-1]
        }
        assert offending_modules == set(), (
            f"{module.__name__} imports publishing modules: {sorted(offending_modules)}"
        )
        assert names_imported & forbidden_names == set(), (
            f"{module.__name__} imports a publisher: "
            f"{sorted(names_imported & forbidden_names)}"
        )

        referenced = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        assert referenced & forbidden_names == set(), (
            f"{module.__name__} references {sorted(referenced & forbidden_names)}"
        )


def test_the_worker_pipeline_has_no_upload_call():
    """§53: no `if ready: youtube.upload(...)` at the end of the render."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "worker" / "app"
    if not root.exists():  # pragma: no cover - worker not present in this checkout
        pytest.skip("worker source not available in this checkout")

    offenders = [
        path
        for path in root.rglob("*.py")
        if "youtube" in path.read_text(encoding="utf-8", errors="ignore").lower()
        and "upload" in path.read_text(encoding="utf-8", errors="ignore").lower()
        and "googleapis" in path.read_text(encoding="utf-8", errors="ignore").lower()
    ]
    assert offenders == []
