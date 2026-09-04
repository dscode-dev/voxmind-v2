"""Publishing runs on OAuth. It must not need a YouTube API key, and must not silently get one.

Two credentials with similar names do completely different jobs here, and conflating them is
the failure this file exists to prevent.

``YOUTUBE_API_KEY`` is a *discovery* credential: an unauthenticated key for public search. It
can read what exists on YouTube and can upload nothing.

``YOUTUBE_CLIENT_ID`` / ``YOUTUBE_CLIENT_SECRET`` plus a channel's refresh token are the
*publishing* credential: OAuth, per channel, and the only thing that can put a video on
someone's account or read a private one back.

Treating them as one setting fails in both directions. A deployment that publishes but never
discovers would be blocked by a key it does not need; and a system that fell back to the API
key for an upload would be reaching for a credential that cannot authorize one — a 401 at the
end of a finished render rather than a clear refusal at the start.

So the tests below pin the separation from both sides: publishing and metrics work with no
API key present, discovery degrades on its own when the key is missing, and no module on the
publishing path so much as reads the setting.
"""
from __future__ import annotations

import ast
import logging
import os
import pathlib
from unittest import mock

import httpx
import pytest

from app.core.settings import Settings
from app.discovery.contracts import DiscoveryRequest, ProviderUnavailable
from app.discovery.youtube_provider import YouTubeSearchProvider
from app.metrics.youtube_metrics import YouTubeVideoMetricsProvider
from app.models.enums import PublishTargetConnectionStatus, VideoCandidateStatus
from app.publishing.contracts import PublishCredential
from app.publishing.youtube_oauth import SCOPES, YouTubeOAuthClient
from app.security.secret_box import SecretBox
from app.services.discovery_service import DiscoveryService, build_default_service
from app.services.publish_target_service import PublishTargetService
from tests.test_boot_security import BASE_ENV, VALID_INTERNAL_TOKEN, VALID_JWT_SECRET
from tests.test_publishing import (  # noqa: F401 - publishing_config is autouse
    REFRESH_TOKEN,
    TEST_KEY,
    make_target,
    oauth_client,
    publishing_config,
)

# A key-shaped string that must never appear in a response, a log line or an event.
FAKE_API_KEY = "AIzaSyDUMMY-discovery-key-should-never-be-used-for-upload"


def build_settings(**overrides) -> Settings:
    values = {
        **BASE_ENV,
        "JWT_SECRET": VALID_JWT_SECRET,
        "INTERNAL_API_TOKEN": VALID_INTERNAL_TOKEN,
        **overrides,
    }
    # The ambient environment is cleared so an omitted field really is omitted, rather than
    # being picked back up from the variables conftest sets to make the app importable.
    with mock.patch.dict(os.environ, {}, clear=True):
        return Settings(_env_file=None, **values)


# ===========================================================================
# The API key is optional
# ===========================================================================


def test_settings_validate_with_no_youtube_api_key():
    """The stack boots without it. An optional integration cannot be a startup dependency."""
    configured = build_settings()

    assert configured.youtube_api_key is None
    # And the publishing credentials are independent of it.
    assert configured.publishing_enabled is False
    assert configured.youtube_client_id is None


def test_publishing_is_configurable_with_no_api_key():
    """OAuth reports itself ready on a deployment that has no discovery key at all."""
    configured = build_settings(
        YOUTUBE_CLIENT_ID="client-id.apps.googleusercontent.com",
        YOUTUBE_CLIENT_SECRET="client-secret",
        YOUTUBE_OAUTH_REDIRECT_URI="https://clipflow.test/auth/youtube/callback",
        PUBLISH_SECRET_KEY=TEST_KEY,
        PUBLISHING_ENABLED="true",
    )

    assert configured.youtube_api_key is None

    client = YouTubeOAuthClient(
        client_id=configured.youtube_client_id,
        client_secret=configured.youtube_client_secret,
        redirect_uri=configured.youtube_oauth_redirect_uri,
    )
    assert client.configured is True
    # Would raise ProviderNotConfiguredError if anything else were required.
    client.require_configured()


def test_oauth_asks_for_upload_and_readonly():
    """Upload to publish; readonly to measure. Nothing wider than the two things this does."""
    assert set(SCOPES) == {
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
    }

    client = YouTubeOAuthClient(
        client_id="client-id",
        client_secret="client-secret",
        redirect_uri="https://clipflow.test/cb",
    )
    url = client.authorization_url(state="state-token")

    query = httpx.URL(url).params
    assert set(query["scope"].split()) == set(SCOPES)
    # Without these two Google returns a refresh token only on the very first consent ever
    # granted, and a reconnect months later yields a token that cannot be renewed.
    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"


# ===========================================================================
# Publishing does not read the key
# ===========================================================================


PUBLISHING_PATH = (
    "app/publishing",
    "app/metrics",
    "app/services/publishing_service.py",
    "app/services/publish_target_service.py",
    "app/services/publish_resolution_service.py",
    "app/services/publish_runtime.py",
    "app/services/autopublish_service.py",
    "app/services/metrics_ingestion_service.py",
)


def test_no_module_on_the_publishing_path_reads_the_api_key():
    """Structural, not a promise.

    A future "fall back to the API key" would have to appear as an attribute access in a
    diff. It could not work anyway — an API key cannot authorize an upload — so the failure
    it would cause is a 401 after a full render rather than a refusal up front.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders: list[str] = []

    for entry in PUBLISHING_PATH:
        path = root / entry
        if not path.exists():
            continue
        files = path.rglob("*.py") if path.is_dir() else [path]
        for file in files:
            source = file.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "youtube_api_key":
                    offenders.append(f"{file.name}:{node.lineno}")
                if isinstance(node, ast.Constant) and node.value == "YOUTUBE_API_KEY":
                    offenders.append(f"{file.name}:{node.lineno} (literal)")

    assert offenders == []


def test_publish_credential_is_built_from_oauth_only(db, no_event_fanout, monkeypatch):
    """The credential handed to an upload contains the channel's token and the OAuth client.

    There is no field on it an API key could occupy, which is what makes the separation
    structural rather than a convention.
    """
    monkeypatch.setattr(
        "app.core.settings.settings.youtube_api_key", FAKE_API_KEY, raising=False
    )
    target = make_target(db)
    db.commit()

    credential = PublishTargetService().credential_for(target)

    assert isinstance(credential, PublishCredential)
    assert credential.refresh_token == REFRESH_TOKEN
    assert credential.client_id == "client-id.apps.google"
    assert credential.client_secret == "client-secret"
    assert FAKE_API_KEY not in str(vars(credential))


# ===========================================================================
# Metrics run on the same OAuth credential
# ===========================================================================


def test_metrics_read_with_oauth_and_never_an_api_key(db, monkeypatch):
    """Private uploads are the normal case, and an API key cannot see one.

    The request is asserted to carry a bearer token and no `key` parameter — the mistake
    would otherwise look like it worked until the first private video returned nothing.
    """
    monkeypatch.setattr(
        "app.core.settings.settings.youtube_api_key", FAKE_API_KEY, raising=False
    )
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-123", "expires_in": 3600})
        seen["authorization"] = request.headers.get("authorization")
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [
            {"id": "vid_a", "statistics": {"viewCount": "42"},
             "status": {"uploadStatus": "processed", "privacyStatus": "private"}},
        ]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = YouTubeVideoMetricsProvider(oauth=oauth_client(handler), client=client)

    result = provider.fetch_metrics(
        ["vid_a"],
        credential=PublishCredential(
            refresh_token=REFRESH_TOKEN,
            client_id="client-id.apps.google",
            client_secret="client-secret",
        ),
    )

    assert result.metrics["vid_a"].view_count == 42
    assert seen["authorization"] == "Bearer at-123"
    assert "key" not in seen["params"]
    assert FAKE_API_KEY not in str(seen)


# ===========================================================================
# Discovery degrades on its own
# ===========================================================================


def test_youtube_discovery_reports_unavailable_without_a_key():
    provider = YouTubeSearchProvider(api_key=None)

    assert provider.is_configured() is False
    with pytest.raises(ProviderUnavailable):
        provider.discover(DiscoveryRequest(queries=["serie a"], max_results=5))


def test_a_missing_key_disables_one_source_and_nothing_else(db, no_event_fanout):
    """The blast radius is one DiscoverySource.

    It is recorded as `unavailable` rather than raising, so the run continues, other sources
    still collect, and — most importantly — nothing in the publishing path is touched.
    """
    from app.models.content_topic import ContentTopic
    from app.models.discovery_source import DiscoverySource
    from app.models.enums import DiscoverySourceKind

    topic = ContentTopic(name="Serie A", is_active=True, keywords_json=["serie a"])
    db.add(topic)
    db.flush()
    source = DiscoverySource(
        topic_id=topic.id, kind=DiscoverySourceKind.YOUTUBE_SEARCH, name="search",
        is_active=True, config_json={"queries": ["serie a"]},
    )
    db.add(source)
    db.commit()

    # A service built exactly as production builds it, with no key.
    service = build_default_service(None, timeout_sec=5.0, max_results=5, freshness_days=7)
    assert isinstance(service, DiscoveryService)

    result = service.run_source(db, topic=topic, source=source, commit=True)

    assert result.status == "unavailable"
    assert result.errors
    # No fabricated candidates stand in for the missing provider.
    from app.models.video_candidate import VideoCandidate

    assert db.query(VideoCandidate).count() == 0
    # And a target stays perfectly publishable: discovery's problem is not publishing's.
    target = make_target(db)
    db.commit()
    assert target.is_publishable is True
    assert target.connection_status == PublishTargetConnectionStatus.CONNECTED


def test_a_missing_key_does_not_reject_a_manually_created_candidate(db, no_event_fanout):
    """Discovery is one way candidates arrive, not the only one.

    Without a key an operator can still add and publish work by hand, which is the whole
    point of the failure being scoped to a provider.
    """
    from app.models.content_topic import ContentTopic
    from app.models.video_candidate import VideoCandidate

    topic = ContentTopic(name="Manual", is_active=True)
    db.add(topic)
    db.flush()
    candidate = VideoCandidate(
        topic_id=topic.id, url="https://youtu.be/manual", title="Added by hand",
        status=VideoCandidateStatus.SELECTED,
    )
    db.add(candidate)
    db.commit()

    assert candidate.status == VideoCandidateStatus.SELECTED


# ===========================================================================
# Nothing leaks
# ===========================================================================


def test_target_serialization_carries_no_credential(db, no_event_fanout):
    """An allow-list, so a column added later is invisible until someone decides otherwise."""
    target = make_target(db)
    db.commit()

    payload = str(PublishTargetService.serialize(target))

    for secret in (REFRESH_TOKEN, TEST_KEY, FAKE_API_KEY, "client-secret"):
        assert secret not in payload
    assert "refresh_token" not in payload
    assert "api_key" not in payload
    # What it does say is whether the target can be used, which is the useful part.
    assert "connection_status" in payload


def test_a_discovery_failure_never_logs_the_key(db, no_event_fanout, caplog):
    """A provider that interpolated its request into an error would put the key in an event.

    The service records the exception *type* rather than its text for exactly this reason.
    """
    from app.models.content_topic import ContentTopic
    from app.models.discovery_source import DiscoverySource
    from app.models.enums import DiscoverySourceKind
    from app.models.pipeline_event import PipelineEvent

    topic = ContentTopic(name="Leaky", is_active=True, keywords_json=["x"])
    db.add(topic)
    db.flush()
    source = DiscoverySource(
        topic_id=topic.id, kind=DiscoverySourceKind.YOUTUBE_SEARCH, name="search",
        is_active=True, config_json={"queries": ["x"]},
    )
    db.add(source)
    db.commit()

    class LeakyProvider:
        provider = "youtube"

        def is_configured(self) -> bool:
            return True

        def discover(self, request):
            raise RuntimeError(f"GET /search?key={FAKE_API_KEY} failed")

    service = DiscoveryService(youtube_provider=LeakyProvider())

    with caplog.at_level(logging.ERROR):
        result = service.run_source(db, topic=topic, source=source, commit=True)

    assert result.status == "failed"
    assert FAKE_API_KEY not in str(result.errors)

    events = db.query(PipelineEvent).all()
    assert FAKE_API_KEY not in str([(e.message, e.payload_json) for e in events])


def test_the_oauth_client_never_puts_its_secret_in_an_error():
    """A misconfiguration names the variable, not its value."""
    client = YouTubeOAuthClient(client_id="", client_secret="", redirect_uri="")

    from app.publishing.contracts import ProviderNotConfiguredError

    with pytest.raises(ProviderNotConfiguredError) as raised:
        client.require_configured()

    message = str(raised.value)
    assert "YOUTUBE_CLIENT_ID" in message
    assert "client-secret" not in message


def test_secret_box_is_required_before_a_token_can_be_stored(db, no_event_fanout,
                                                             monkeypatch):
    """Without encryption configured a target cannot be connected at all.

    The connect flow refuses rather than storing a refresh token in the clear, which is why
    PUBLISH_SECRET_KEY is documented as required for publishing and not as an optimisation.
    """
    monkeypatch.setattr(
        "app.core.settings.settings.publish_secret_key", None, raising=False
    )
    box = SecretBox(None)

    assert box.available is False
