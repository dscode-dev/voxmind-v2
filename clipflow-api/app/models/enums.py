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
    PROMPT = "prompt"
    AI_RESPONSE = "ai_response"


class AssetStatus(str, enum.Enum):
    READY = "ready"
    PROCESSING = "processing"
    FAILED = "failed"
    DELETED = "deleted"