from __future__ import annotations

import enum


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    CUSTOMER = "customer"


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    PENDING_VERIFICATION = "pending_verification"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class BillingProvider(str, enum.Enum):
    STRIPE = "stripe"
    MERCADOPAGO = "mercadopago"
    MANUAL = "manual"


class PurchaseStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    CANCELED = "canceled"
    EXPIRED = "expired"


class ProductType(str, enum.Enum):
    VIDEO_UP_TO_2H = "video_up_to_2h"
    VIDEO_UP_TO_4H = "video_up_to_4h"


class JobStatus(str, enum.Enum):
    PENDING_PAYMENT = "pending_payment"
    QUEUED = "queued"
    PREPARING = "preparing"
    AWAITING_MANUAL_LLM = "awaiting_manual_llm"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class JobInputMode(str, enum.Enum):
    MANUAL_PROMPT = "manual_prompt"
    DIRECT_AGENT = "direct_agent"


class JobSourceType(str, enum.Enum):
    YOUTUBE_URL = "youtube_url"
    DIRECT_UPLOAD = "direct_upload"


class ClipAssetType(str, enum.Enum):
    SHORT_CLIP = "short_clip"
    MERGED_CLIP = "merged_clip"
    THUMBNAIL = "thumbnail"
    TRANSCRIPT = "transcript"
    TRANSCRIPT_WITH_SPEAKERS = "transcript_with_speakers"
    SPEAKER_TURNS = "speaker_turns"
    CANDIDATES = "candidates"
    PROMPT = "prompt"
    AI_RESPONSE = "ai_response"
    QA_REPORT = "qa_report"
    DELIVERY_PACKAGE = "delivery_package"
    ARTIFACTS_MANIFEST = "artifacts_manifest"
    RUNTIME_STATUS = "runtime_status"

class AssetStatus(str, enum.Enum):
    READY = "ready"
    PROCESSING = "processing"
    FAILED = "failed"
    DELETED = "deleted"
    
class JobEventType(str, enum.Enum):

    JOB_CREATED = "job_created"

    DOWNLOAD_STARTED = "download_started"
    DOWNLOAD_FINISHED = "download_finished"

    TRANSCRIPTION_STARTED = "transcription_started"
    TRANSCRIPTION_FINISHED = "transcription_finished"
    DIARIZATION_STARTED = "diarization_started"
    DIARIZATION_FINISHED = "diarization_finished"

    LLM_REQUEST_STARTED = "llm_request_started"
    LLM_REQUEST_FINISHED = "llm_request_finished"

    CUT_GENERATED = "cut_generated"

    RENDER_STARTED = "render_started"
    RENDER_FINISHED = "render_finished"
    QA_STARTED = "qa_started"
    QA_FINISHED = "qa_finished"
    DELIVERY_PACKAGE_READY = "delivery_package_ready"

    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"


class UsageMetricType(str, enum.Enum):

    GPU_SECONDS = "gpu_seconds"

    CPU_SECONDS = "cpu_seconds"

    STORAGE_BYTES = "storage_bytes"

    LLM_TOKENS = "llm_tokens"

    TRANSCRIPTION_SECONDS = "transcription_seconds"


# =====================================================
# V2 — Autonomous pipeline (PipelineJob lineage)
# =====================================================


class PipelineState(str, enum.Enum):
    """Granular state machine for autonomous PipelineJobs (brief §PIPELINE STATE MACHINE)."""

    DISCOVERED = "discovered"
    SELECTED = "selected"
    # Enqueued and waiting for a worker to claim it. Added in PR-STATE-01: the machine had
    # no way to say "accepted, not yet running", so a job's first observable state was
    # DOWNLOADING — after the work had already begun.
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    ANALYZING = "analyzing"
    PROMPT_BUILDING = "prompt_building"
    WAITING_AI = "waiting_ai"
    AI_COMPLETED = "ai_completed"
    RENDERING = "rendering"
    RENDERED = "rendered"
    # The run finished and its output passed the PR-QA-01 technical gate.
    READY_TO_PUBLISH = "ready_to_publish"
    # The run finished but its output needs a human before it can go anywhere. Added in
    # PR-STATE-01 so a blocked render is not filed under a state whose name asserts it is
    # ready. This is a workflow state (the workflow now waits on a person), not a QA verdict
    # — the verdict itself stays in publication_eligibility, and QA_AUTO_READY /
    # QA_NEEDS_REVIEW / QA_BLOCKED are deliberately NOT states.
    REVIEW_REQUIRED = "review_required"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELED = "canceled"


class PipelineEventType(str, enum.Enum):
    """Coarse classification for the generic PipelineEvent stream."""

    STATE_CHANGED = "state_changed"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    RETRY = "retry"
    HEARTBEAT = "heartbeat"


class VideoCandidateStatus(str, enum.Enum):
    DISCOVERED = "discovered"
    RANKED = "ranked"
    SELECTED = "selected"
    REJECTED = "rejected"
    CONSUMED = "consumed"


class DiscoverySourceKind(str, enum.Enum):
    YOUTUBE_TRENDING = "youtube_trending"
    YOUTUBE_SEARCH = "youtube_search"
    NEWS = "news"
    RSS = "rss"
    MANUAL = "manual"


class GeneratedAssetKind(str, enum.Enum):
    CLIP = "clip"
    FINAL_VIDEO = "final_video"
    THUMBNAIL = "thumbnail"
    THUMBNAIL_PROMPT = "thumbnail_prompt"
    TITLE = "title"
    DESCRIPTION = "description"
    HASHTAGS = "hashtags"
    SUBTITLES = "subtitles"
    METADATA = "metadata"


class PublishPlatform(str, enum.Enum):
    TELEGRAM = "telegram"
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"


class PublishAttemptStatus(str, enum.Enum):
    """The outcome of one logical publication.

    Extended by PR-PUBLISH-01. The original PENDING/SUCCEEDED/FAILED could not express the
    distinction publishing actually turns on: a 503 and a connection dropped after the last
    byte was sent both looked like "failed", and retrying the second one duplicates a public
    video. Failure is now split by what may safely happen next.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    # Safe to try again: nothing was accepted by the provider, or the provider explicitly
    # told us to come back later.
    FAILED_RETRYABLE = "failed_retryable"
    # Trying again would fail identically - bad metadata, revoked credentials, rejected
    # media. Needs a human to change something first.
    FAILED_FINAL = "failed_final"
    # The upload may or may not have succeeded: bytes were sent and the response was lost.
    # This is the state that must never be retried automatically.
    UNKNOWN = "unknown"
    # An UNKNOWN an operator has taken ownership of. Distinct from UNKNOWN so a queue of
    # "someone is looking at this" does not hide inside a queue of "nobody has yet".
    NEEDS_MANUAL_RESOLUTION = "needs_manual_resolution"
    CANCELED = "canceled"
    # Kept only so the enum can still read rows written before PR-PUBLISH-01. Nothing
    # writes it: there were no writers, so in practice there are no such rows either.
    FAILED = "failed"


class PublishRetryability(str, enum.Enum):
    """What the caller is allowed to do next. Deliberately separate from the attempt status.

    Status says where the attempt ended; retryability says what may happen to it. Keeping
    them apart is what stops "failed" from being read as "try again" by the next person to
    touch this code.
    """

    RETRYABLE = "retryable"
    NOT_RETRYABLE = "not_retryable"
    # Neither. Requires a human decision before anything else happens.
    REQUIRES_MANUAL_RESOLUTION = "requires_manual_resolution"


class PublishTargetConnectionStatus(str, enum.Enum):
    """Whether the stored credential can still be used."""

    # Never connected, or explicitly disconnected. No refresh token is held.
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    # The refresh token was rejected (revoked, expired, invalid_grant). Retrying every job
    # against it would burn quota and log noise forever; an operator must reconnect.
    RECONNECT_REQUIRED = "reconnect_required"


class ConnectedNodeStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class AIExecutionStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
