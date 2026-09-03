"""What the domain hands a publisher, and what it gets back.

The point of this module is that ``PublishingService`` never imports anything YouTube. It
builds a ``PublishRequest``, calls ``Publisher.publish``, and reads a ``PublishResult``. A
second provider is then an adapter, not a second copy of the orchestration.

**The outcome vocabulary is the important part.** Most integrations model an upload as
success-or-failure. That is exactly the model that duplicates videos: a connection dropped
after the final byte is not a failure, it is an *unanswered question*, and treating it as a
failure makes retrying it look reasonable. So the result carries three shapes of ending —
succeeded, failed (with a retryability the adapter decides), and unknown — and the domain is
written so that the third one stops.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, BinaryIO, Protocol

from app.models.enums import PublishRetryability


class PublishOutcome:
    """The three ways an upload can end. Strings, so they serialise into events as-is."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Bytes were sent and we do not know whether the provider accepted them.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PublishMedia:
    """The bytes to upload, as a stream the adapter reads and never a blob it holds.

    ``open_stream`` is a callable rather than an already-open handle because a resumable
    upload may need to re-read from an offset after a broken connection, and a consumed
    stream cannot do that.
    """

    identity: str
    storage_key: str
    size_bytes: int
    content_type: str
    open_stream: Any  # Callable[[], BinaryIO] — typed loosely to keep the dataclass hashable

    def open(self) -> BinaryIO:
        return self.open_stream()


@dataclass(frozen=True)
class PublishMetadata:
    """Exactly the editorial fields that get sent. Nothing here is read from the database.

    The adapter receives this and only this, so it cannot reach into a model and publish a
    field nobody reviewed. Everything is already resolved and validated by the time it is
    built — precedence, truncation policy and length limits are the domain's business, not
    the provider's.
    """

    title: str
    description: str
    tags: list[str]
    privacy: str
    category_id: str | None
    language: str | None
    made_for_kids: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "privacy": self.privacy,
            "category_id": self.category_id,
            "language": self.language,
            "made_for_kids": self.made_for_kids,
        }


@dataclass(frozen=True)
class PublishCredential:
    """A refresh token, already decrypted, plus what is needed to spend it.

    Passed in rather than fetched by the adapter: decryption is a security decision, and it
    belongs in one place that can be audited, not inside a provider client.
    """

    refresh_token: str
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class PublishRequest:
    """One upload, fully specified."""

    attempt_id: str
    pipeline_job_id: str
    publish_target_id: str
    media: PublishMedia
    metadata: PublishMetadata
    credential: PublishCredential
    # A resumable session from a previous attempt, if one survived. Absent means "start a
    # new session".
    resume_session_uri: str | None = None


@dataclass
class PublishResult:
    """What came back. Absent facts stay ``None`` — nothing here is inferred or invented."""

    provider: str
    outcome: str
    external_id: str | None = None
    external_url: str | None = None
    published_at: datetime | None = None
    privacy: str | None = None
    # Only set when outcome is FAILED. UNKNOWN deliberately has no retryability: the whole
    # point is that the caller must not decide on its own.
    retryability: PublishRetryability | None = None
    error_code: str | None = None
    error_message: str | None = None
    bytes_uploaded: int | None = None
    # Kept so a resumed upload can continue rather than restart.
    session_uri: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.outcome == PublishOutcome.SUCCEEDED

    @property
    def is_unknown(self) -> bool:
        return self.outcome == PublishOutcome.UNKNOWN


class Publisher(Protocol):
    """The port. One method, because publishing is one operation."""

    provider: str

    def publish(self, request: PublishRequest) -> PublishResult:
        ...


class ProviderNotConfiguredError(RuntimeError):
    """The provider has no usable configuration in this deployment.

    Separate from a failed upload: "no client id is set" is an operator problem discovered
    before any byte moves, and reporting it as an upload failure would put it in the retry
    path where it does not belong.
    """
