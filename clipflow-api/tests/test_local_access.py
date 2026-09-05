"""Local bootstrap login, and editorial metadata for publication.

Two capabilities that share one property: both are conveniences that must not become holes.

The login one is the sharper of the two. A fixed code makes a machine with no SMS usable, and
the mechanism it replaces resolved that code for *every* login — while `/auth/start` creates a
user for any unknown number. Together that was a way to mint an account for an arbitrary phone
and sign in as it. The tests below pin the scoping from every side: right phone, wrong phone,
wrong code, feature off, wrong environment.

The metadata one must never cost a video. A model that times out, returns nonsense, or is not
configured at all has to leave a publishable run behind — a re-render because nobody could
write a title would be an absurd outcome, and the tests say so.
"""
from __future__ import annotations

import json
import logging
import os
from unittest import mock

import httpx
import pytest
from pydantic import ValidationError

from app.core.settings import Settings, settings
from app.models.enums import PipelineState, UserRole, UserStatus
from app.models.user import User
from app.publishing.metadata import MAX_TAG_CHARS, MAX_TITLE_CHARS, resolve
from app.publishing.metadata_ai import (
    INVALID,
    OK,
    UNAVAILABLE,
    ClipContext,
    NullMetadataGenerator,
    OpenAIMetadataGenerator,
    build_metadata_generator,
)
from app.security.bootstrap_auth import bootstrap_admin_phone, resolve_bootstrap_code
from app.security.phone import normalize_phone_number
from app.services.bootstrap_service import BootstrapService
from app.services.publication_metadata_service import (
    METADATA_KEY,
    PublicationMetadataService,
)
from app.services.publishing_service import MediaItem
from tests.conftest import make_run
from tests.test_boot_security import BASE_ENV, VALID_INTERNAL_TOKEN, VALID_JWT_SECRET

BOOTSTRAP_PHONE = "+5581999990000"
OTHER_PHONE = "+5581988887777"
BOOTSTRAP_CODE = "246810"
FAKE_OPENAI_KEY = "sk-proj-DUMMY-should-never-appear-anywhere"


def build_settings(**overrides) -> Settings:
    values = {
        **BASE_ENV,
        "JWT_SECRET": VALID_JWT_SECRET,
        "INTERNAL_API_TOKEN": VALID_INTERNAL_TOKEN,
        **overrides,
    }
    with mock.patch.dict(os.environ, {}, clear=True):
        return Settings(_env_file=None, **values)


@pytest.fixture()
def bootstrap_config(monkeypatch):
    """A development machine with the local login switched on."""
    monkeypatch.setattr(settings, "environment", "development", raising=False)
    monkeypatch.setattr(settings, "bootstrap_admin_enabled", True, raising=False)
    monkeypatch.setattr(settings, "bootstrap_admin_auth_code", BOOTSTRAP_CODE, raising=False)
    monkeypatch.setattr(
        settings, "default_admin_phone_number", BOOTSTRAP_PHONE, raising=False
    )
    monkeypatch.setattr(settings, "default_admin_full_name", "ClipFlow Admin", raising=False)
    monkeypatch.setattr(settings, "default_admin_credits", 100, raising=False)


@pytest.fixture()
def own_session(db):
    """The factory the metadata service persists through.

    Generation commits on a session of its own — see `_commit_results` — because the caller
    may be a dry run that never commits. Binding it to the test's engine keeps that real
    without letting it reach for the deployment database.
    """
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=db.get_bind(), future=True, expire_on_commit=False)


# ===========================================================================
# Bootstrap
# ===========================================================================


def test_an_empty_database_gets_exactly_one_admin(db, bootstrap_config):
    BootstrapService().ensure_default_admin(db)

    admins = db.query(User).all()
    assert len(admins) == 1
    assert admins[0].role == UserRole.ADMIN
    assert admins[0].status == UserStatus.ACTIVE


def test_running_bootstrap_again_does_not_create_a_second_user(db, bootstrap_config):
    """Startup runs on every container restart; it must converge, not accumulate."""
    service = BootstrapService()
    service.ensure_default_admin(db)
    service.ensure_default_admin(db)
    service.ensure_default_admin(db)

    assert db.query(User).count() == 1


def test_bootstrap_restores_admin_properties_on_an_existing_user(db, bootstrap_config):
    """The phone may already exist as an ordinary account — that is the upgrade path."""
    db.add(
        User(
            phone_number=normalize_phone_number(BOOTSTRAP_PHONE, "BR"),
            full_name="Existing",
            role=UserRole.CUSTOMER,
            status=UserStatus.ACTIVE,
            credits=0,
            token_version=1,
        )
    )
    db.commit()

    BootstrapService().ensure_default_admin(db)

    user = db.query(User).one()
    assert user.role == UserRole.ADMIN
    # A name the person chose is not overwritten by the configured default.
    assert user.full_name == "Existing"


def test_the_phone_is_stored_in_the_form_the_login_will_look_up(db, bootstrap_config,
                                                               monkeypatch):
    """`+55 81 99999-0000`, `5581999990000` and `(81) 99999-0000` are one account.

    Without canonicalising the configured value, the bootstrap would create a user the login
    path could never find — the account would exist and be unreachable.
    """
    # Every spelling that carries the country code explicitly. `5581...` without the `+` is
    # deliberately not among them: the shared normalizer prefixes +55 to it and yields a
    # different number entirely. That quirk is pre-existing and identical on the login path,
    # so the two sides still agree — it is recorded as debt, not worked around here.
    for written in ("+55 81 99999-0000", "+5581999990000", "+55 (81) 99999-0000"):
        monkeypatch.setattr(settings, "default_admin_phone_number", written, raising=False)
        BootstrapService().ensure_default_admin(db)

    assert db.query(User).count() == 1
    assert db.query(User).one().phone_number == normalize_phone_number(
        BOOTSTRAP_PHONE, "BR"
    )
    assert bootstrap_admin_phone() == normalize_phone_number(BOOTSTRAP_PHONE, "BR")


# ===========================================================================
# The fixed code is scoped to one account
# ===========================================================================


def test_the_bootstrap_phone_gets_the_configured_code(bootstrap_config):
    assert resolve_bootstrap_code(normalize_phone_number(BOOTSTRAP_PHONE, "BR")) == (
        BOOTSTRAP_CODE
    )


def test_any_other_phone_gets_nothing(bootstrap_config):
    """The regression this file exists for.

    The mechanism this replaced returned the fixed code for every login, and `/auth/start`
    creates a user for an unknown number — so it was a way to create an account for any phone
    and sign in as it.
    """
    assert resolve_bootstrap_code(normalize_phone_number(OTHER_PHONE, "BR")) is None
    assert resolve_bootstrap_code(None) is None
    assert resolve_bootstrap_code("") is None


def test_the_feature_switched_off_yields_nothing(bootstrap_config, monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_admin_enabled", False, raising=False)

    assert resolve_bootstrap_code(normalize_phone_number(BOOTSTRAP_PHONE, "BR")) is None


def test_a_non_development_environment_yields_nothing(bootstrap_config, monkeypatch):
    """The fail-safe, independent of the boot-time refusal below."""
    monkeypatch.setattr(settings, "environment", "production", raising=False)

    assert resolve_bootstrap_code(normalize_phone_number(BOOTSTRAP_PHONE, "BR")) is None


def test_an_empty_code_yields_nothing(bootstrap_config, monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_admin_auth_code", "   ", raising=False)

    assert resolve_bootstrap_code(normalize_phone_number(BOOTSTRAP_PHONE, "BR")) is None


def test_production_refuses_to_start_with_the_local_login_enabled():
    """A fixed admin code in production is a shared password. Caught at boot, not at login."""
    with pytest.raises(ValidationError) as raised:
        build_settings(
            ENVIRONMENT="production",
            BOOTSTRAP_ADMIN_ENABLED="true",
            BOOTSTRAP_ADMIN_AUTH_CODE=BOOTSTRAP_CODE,
        )

    assert "BOOTSTRAP_ADMIN_ENABLED" in str(raised.value)


def test_enabling_it_without_a_code_is_refused():
    """Otherwise the feature is silently inert and the operator thinks it is on."""
    with pytest.raises(ValidationError):
        build_settings(ENVIRONMENT="development", BOOTSTRAP_ADMIN_ENABLED="true")


def test_it_is_off_by_default():
    configured = build_settings()

    assert configured.bootstrap_admin_enabled is False
    assert configured.resolve_bootstrap_auth_code() is None


def test_absent_configuration_leaves_the_application_alone():
    """No envs set: the app runs, the login is ordinary."""
    configured = build_settings(ENVIRONMENT="development")

    assert configured.resolve_bootstrap_auth_code() is None


def test_the_code_is_never_returned_or_logged(db, bootstrap_config, caplog, no_event_fanout):
    """It is a secret. It goes into an OTP hash and nowhere else."""
    from app.services.otp_service import hash_otp, verify_otp

    with caplog.at_level(logging.DEBUG):
        BootstrapService().ensure_default_admin(db)
        code = resolve_bootstrap_code(normalize_phone_number(BOOTSTRAP_PHONE, "BR"))

    hashed = hash_otp(code)
    assert verify_otp(code, hashed)
    # Stored as a hash like any other code, never in the clear.
    assert BOOTSTRAP_CODE not in hashed
    assert BOOTSTRAP_CODE not in caplog.text

    user = db.query(User).one()
    assert BOOTSTRAP_CODE not in str(
        {c.name: getattr(user, c.name) for c in user.__table__.columns}
    )


# ===========================================================================
# Metadata generation
# ===========================================================================


def _item(index: int, video: dict | None = None) -> MediaItem:
    return MediaItem(
        identity=f"final_clips/final_clip_{index:02d}.mp4",
        storage_key=f"jobs/x/final_clips/final_clip_{index:02d}.mp4",
        video_index=index,
        video=video or {},
    )


class StubGenerator:
    """A generator whose answers the test dictates, recording every context it saw."""

    def __init__(self, answers=None, available=True):
        from app.publishing.metadata_ai import GeneratedMetadata, MetadataResult

        self._answers = list(answers or [])
        self._available = available
        self.contexts: list[ClipContext] = []
        self._GeneratedMetadata = GeneratedMetadata
        self._MetadataResult = MetadataResult

    def is_available(self) -> bool:
        return self._available

    def generate(self, context: ClipContext):
        self.contexts.append(context)
        if self._answers:
            return self._answers.pop(0)
        return self._MetadataResult(
            status=OK,
            metadata=self._GeneratedMetadata(
                title=f"Clipe {context.video_index}",
                description="Contexto do corte.",
                tags=["futebol", "serie a"],
            ),
            provider="stub",
            model="stub-1",
            latency_ms=10,
        )


class StubArtifacts:
    def __init__(self, package=None):
        self._package = package or {}

    def load_json(self, key):  # noqa: ANN001
        return self._package


def test_each_clip_of_a_run_gets_its_own_metadata(db, own_session, no_event_fanout):
    """Three cuts of one match are three subjects.

    One description applied to all of them would be wrong about at least two.
    """
    job = make_run(db, state=PipelineState.READY_TO_PUBLISH)
    db.commit()
    generator = StubGenerator()
    service = PublicationMetadataService(
        generator=generator, artifacts=StubArtifacts(), session_factory=own_session
    )

    items = [
        _item(1, {"post": {"hook": "o gol de empate"}}),
        _item(2, {"post": {"hook": "a expulsao"}}),
        _item(3, {"post": {"hook": "a defesa no fim"}}),
    ]
    result = service.ensure(db, job, items)

    assert set(result) == {1, 2, 3}
    assert len({row["title"] for row in result.values()}) == 3
    # Each call saw that clip's own context, not the run's.
    assert [c.clip_hook for c in generator.contexts] == [
        "o gol de empate", "a expulsao", "a defesa no fim",
    ]


def test_generation_is_not_repeated_for_a_clip_that_already_has_metadata(
    db, own_session, no_event_fanout
):
    """A retry must republish under the title the first attempt committed to."""
    job = make_run(db, state=PipelineState.READY_TO_PUBLISH)
    db.commit()
    generator = StubGenerator()
    service = PublicationMetadataService(
        generator=generator, artifacts=StubArtifacts(), session_factory=own_session
    )

    service.ensure(db, job, [_item(1)])
    first = dict((job.metadata_json or {})[METADATA_KEY]["1"])
    service.ensure(db, job, [_item(1)])

    assert len(generator.contexts) == 1
    assert (job.metadata_json or {})[METADATA_KEY]["1"] == first


def test_context_is_built_from_persisted_facts(db, own_session, no_event_fanout):
    from app.models.content_topic import ContentTopic
    from app.models.enums import VideoCandidateStatus
    from app.models.video_candidate import VideoCandidate

    topic = ContentTopic(name="Serie A", is_active=True, keywords_json=["serie a", "milan"])
    db.add(topic)
    db.flush()
    candidate = VideoCandidate(
        topic_id=topic.id, url="https://youtu.be/x", title="Milan 3-1 Inter | Highlights",
        channel="Serie A", status=VideoCandidateStatus.CONSUMED,
    )
    db.add(candidate)
    db.flush()
    job = make_run(db, state=PipelineState.READY_TO_PUBLISH, topic_id=topic.id,
                   candidate_id=candidate.id)
    db.commit()

    generator = StubGenerator()
    PublicationMetadataService(
        generator=generator, artifacts=StubArtifacts(), session_factory=own_session
    ).ensure(db, job, [_item(1, {"transcript": "o Milan abriu o placar aos 12 minutos"})])

    context = generator.contexts[0]
    assert context.topic_name == "Serie A"
    assert context.source_title == "Milan 3-1 Inter | Highlights"
    assert context.source_channel == "Serie A"
    assert "12 minutos" in (context.transcript_excerpt or "")
    assert context.is_thin is False


def test_a_clip_with_no_context_is_flagged_as_thin():
    """Thin context must produce caution, not a confident invention."""
    assert ClipContext(video_index=1, topic_name="Serie A").is_thin is True
    assert ClipContext(video_index=1, transcript_excerpt="...").is_thin is False


# ---------------------------------------------------------------- failure paths


@pytest.mark.parametrize(
    "result_status, error",
    [(UNAVAILABLE, "no_api_key"), ("failed", "ConnectTimeout"), (INVALID, "ValidationError")],
)
def test_a_generation_failure_leaves_the_run_publishable(db, own_session, no_event_fanout,
                                                         result_status, error):
    """The video is rendered and valid. A missing title cannot be a reason to lose it."""
    from app.publishing.metadata_ai import MetadataResult

    job = make_run(db, state=PipelineState.READY_TO_PUBLISH)
    db.commit()
    generator = StubGenerator(
        answers=[MetadataResult(status=result_status, error=error)]
    )
    service = PublicationMetadataService(
        generator=generator, artifacts=StubArtifacts(), session_factory=own_session
    )

    result = service.ensure(db, job, [_item(1)])

    assert result == {}
    assert job.state == PipelineState.READY_TO_PUBLISH
    assert METADATA_KEY not in (job.metadata_json or {})


def test_an_unconfigured_deployment_generates_nothing_and_says_so(db, own_session,
                                                                 no_event_fanout):
    job = make_run(db, state=PipelineState.READY_TO_PUBLISH)
    db.commit()
    service = PublicationMetadataService(
        generator=NullMetadataGenerator(),
        artifacts=StubArtifacts(),
        session_factory=own_session,
    )

    assert service.ensure(db, job, [_item(1)]) == {}


def test_a_missing_key_builds_the_null_generator():
    assert isinstance(
        build_metadata_generator(None, model="m", timeout_sec=1.0), NullMetadataGenerator
    )
    assert isinstance(
        build_metadata_generator("  ", model="m", timeout_sec=1.0), NullMetadataGenerator
    )
    assert isinstance(
        build_metadata_generator("sk-x", model="m", timeout_sec=1.0),
        OpenAIMetadataGenerator,
    )


def test_a_timeout_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow")

    generator = OpenAIMetadataGenerator(
        FAKE_OPENAI_KEY, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = generator.generate(ClipContext(video_index=1))

    assert result.ok is False
    assert result.error == "ConnectTimeout"


def test_an_unparseable_answer_is_rejected_rather_than_repaired():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "not json at all"}}]}
        )

    generator = OpenAIMetadataGenerator(
        FAKE_OPENAI_KEY, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = generator.generate(ClipContext(video_index=1))

    assert result.status == INVALID
    assert result.metadata is None


def test_a_provider_error_body_is_never_carried_out():
    """Google and OpenAI both reflect the request into some errors, and it carries the key."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": f"Incorrect API key: {FAKE_OPENAI_KEY}"}}
        )

    generator = OpenAIMetadataGenerator(
        FAKE_OPENAI_KEY, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = generator.generate(ClipContext(video_index=1))

    assert result.error == "http_401"
    assert FAKE_OPENAI_KEY not in str(result)
    assert FAKE_OPENAI_KEY not in str(result.provenance())


def test_the_api_key_never_reaches_a_stored_row(db, own_session, no_event_fanout,
                                               monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", FAKE_OPENAI_KEY, raising=False)
    job = make_run(db, state=PipelineState.READY_TO_PUBLISH)
    db.commit()

    PublicationMetadataService(
        generator=StubGenerator(), artifacts=StubArtifacts(), session_factory=own_session
    ).ensure(db, job, [_item(1)])

    assert FAKE_OPENAI_KEY not in json.dumps(job.metadata_json or {})


# --------------------------------------------------------------- persistence


def test_a_generation_survives_a_caller_that_never_commits(tmp_path, no_event_fanout):
    """The regression a real run found.

    Preparing a publication runs a dry pass first, and that pass decides nothing — so it
    correctly never commits. Written through the caller, the metadata that had really been
    generated and the record of the call that produced it were both rolled back with it: the
    next real publish paid OpenAI again for the same clips, and `/admin/ai/status` reported
    `last_execution: null` about a call that had definitely happened.

    Two real connections here, not the shared in-memory one — on a single connection a
    rollback and a commit are indistinguishable and the test would pass either way.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 - registers every mapper
    from app.db.base import Base
    from app.models.ai_execution import AIExecution
    from app.models.pipeline_job import PipelineJob

    engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    setup = factory()
    job_id = make_run(setup, state=PipelineState.READY_TO_PUBLISH).id
    setup.commit()
    setup.close()

    caller = factory()
    caller_job = caller.query(PipelineJob).filter(PipelineJob.id == job_id).one()
    PublicationMetadataService(
        generator=StubGenerator(), artifacts=StubArtifacts(), session_factory=factory
    ).ensure(caller, caller_job, [_item(1)])
    caller.rollback()  # what a dry run does
    caller.close()

    reader = factory()
    try:
        stored = reader.query(PipelineJob).filter(PipelineJob.id == job_id).one()
        assert METADATA_KEY in (stored.metadata_json or {})
        executions = reader.query(AIExecution).all()
        assert len(executions) == 1
        assert executions[0].purpose == "publication_metadata"
    finally:
        reader.close()
        engine.dispose()


def test_every_call_is_recorded_including_the_ones_that_failed(db, own_session,
                                                              no_event_fanout):
    """"Is the AI working?" is answered from calls, not from configuration.

    A failed call is the more informative of the two, so it is recorded with the same care —
    with the provider, the model and a sanitised reason, and never the prompt or the answer.
    """
    from app.models.ai_execution import AIExecution
    from app.models.enums import AIExecutionStatus
    from app.publishing.metadata_ai import MetadataResult

    job = make_run(db, state=PipelineState.READY_TO_PUBLISH)
    db.commit()

    PublicationMetadataService(
        generator=StubGenerator(
            answers=[MetadataResult(status="failed", error="ConnectTimeout", provider="openai")]
        ),
        artifacts=StubArtifacts(),
        session_factory=own_session,
    ).ensure(db, job, [_item(1)])

    execution = db.query(AIExecution).one()
    assert execution.status == AIExecutionStatus.FAILED
    assert execution.error_message == "ConnectTimeout"
    assert execution.payload_json.get("video_index") == 1


# ------------------------------------------------------------------ validation


def test_generated_metadata_still_goes_through_the_publishing_contract():
    """Structured output is not trusted output.

    An over-long title is a blocked publication, exactly as it would be if a human had
    written one — the model does not get a private set of limits.
    """
    from app.publishing.metadata import MetadataValidationError

    with pytest.raises(MetadataValidationError):
        resolve(
            video={"post": {"title": "x" * (MAX_TITLE_CHARS + 40)}},
            package={},
            target_config={"default_privacy": "private"},
            overrides=None,
        )


def test_over_long_tags_are_dropped_by_the_existing_rules():
    resolved = resolve(
        video={"post": {"title": "Um titulo", "hashtags": ["ok", "y" * (MAX_TAG_CHARS + 5)]}},
        package={},
        target_config={"default_privacy": "private"},
        overrides=None,
    )

    assert "ok" in resolved.metadata.tags
    assert all(len(tag) <= MAX_TAG_CHARS for tag in resolved.metadata.tags)


# ------------------------------------------------------------------ precedence


def test_an_explicit_request_beats_generated_metadata():
    """The operator asked for a title. Nothing downstream may quietly replace it."""
    resolved = resolve(
        video={"post": {"title": "Titulo gerado pelo modelo"}},
        package={},
        target_config={"default_privacy": "private"},
        overrides={"title": "Titulo escolhido pelo operador"},
    )

    assert resolved.metadata.title == "Titulo escolhido pelo operador"
    assert resolved.sources["title"] == "request"


def test_the_model_never_decides_privacy():
    """The invariant. Distribution is a policy decision; the model writes prose.

    There is no privacy field on the way out of the generator, and a title that says
    otherwise changes nothing.
    """
    from app.publishing.metadata_ai import GeneratedMetadata

    assert "privacy" not in GeneratedMetadata.model_fields

    resolved = resolve(
        video={"post": {"title": "PUBLIC agora!", "description": "privacy: public"}},
        package={"privacy": "public"},
        target_config={"default_privacy": "private"},
        overrides=None,
    )

    assert resolved.metadata.privacy == "private"
    assert resolved.sources["privacy"] == "target_default"


def test_generated_text_only_fills_what_the_worker_left_empty():
    from app.services.publishing_service import _with_editorial

    item = _item(1, {"post": {"title": "Titulo do worker"}})
    merged = _with_editorial(
        item, {"title": "Titulo do modelo", "description": "Descricao", "tags": ["a"]}
    )

    # The worker's own words survive; only the gaps are filled.
    assert merged.video["post"]["title"] == "Titulo do worker"
    assert merged.video["post"]["description"] == "Descricao"


def test_merging_nothing_leaves_the_item_untouched():
    from app.services.publishing_service import _with_editorial

    item = _item(1, {"post": {"title": "Titulo"}})
    assert _with_editorial(item, None) is item
