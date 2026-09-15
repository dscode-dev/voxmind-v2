"""Every mapper the application has, imported so SQLAlchemy can resolve relationships by name.

The list is short on purpose. What lives here is the cut-production flow and nothing else:
the operator, the videos it finds, the runs that turn them into cuts, and the publications
those cuts become.
"""
from app.models.user import User
from app.models.clip_job import ClipJob
from app.models.clip_asset import ClipAsset
from app.models.idempotency_key import IdempotencyKey
from app.models.job_event import JobEvent
from app.models.job_lease import JobLease
from app.models.job_queue import JobQueue
from app.models.audit_log import AuditLog

# The autonomous flow: find → select → produce → publish → measure.
from app.models.automation_state import AutomationState
from app.models.oauth_state import OAuthState
from app.models.content_topic import ContentTopic
from app.models.discovery_source import DiscoverySource
from app.models.video_candidate import VideoCandidate
from app.models.pipeline_job import PipelineJob
from app.models.pipeline_event import PipelineEvent
from app.models.publish_target import PublishTarget
from app.models.publish_attempt import PublishAttempt
from app.models.video_performance_snapshot import VideoPerformanceSnapshot
from app.models.ai_execution import AIExecution

__all__ = [
    "User",
    "ClipJob",
    "ClipAsset",
    "IdempotencyKey",
    "JobEvent",
    "JobLease",
    "JobQueue",
    "AuditLog",
    "AutomationState",
    "OAuthState",
    "ContentTopic",
    "DiscoverySource",
    "VideoCandidate",
    "PipelineJob",
    "PipelineEvent",
    "PublishTarget",
    "PublishAttempt",
    "VideoPerformanceSnapshot",
    "AIExecution",
]
