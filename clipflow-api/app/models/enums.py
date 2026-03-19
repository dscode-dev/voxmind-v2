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
