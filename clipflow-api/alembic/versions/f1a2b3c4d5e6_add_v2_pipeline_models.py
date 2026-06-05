"""add v2 pipeline models

Adds the autonomous-pipeline (PipelineJob) lineage: content topics, discovery sources,
video candidates, pipeline jobs + generic pipeline events, generated assets, publish
targets/attempts, connected nodes and AI executions. Touches no existing table.

Revision ID: f1a2b3c4d5e6
Revises: e7b9c4a2d601
Create Date: 2026-06-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e7b9c4a2d601"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Enum value lists use the SQLAlchemy convention of persisting the enum member NAME.
PIPELINE_STATE = sa.Enum(
    "DISCOVERED", "SELECTED", "DOWNLOADING", "DOWNLOADED", "TRANSCRIBING", "TRANSCRIBED",
    "ANALYZING", "PROMPT_BUILDING", "WAITING_AI", "AI_COMPLETED", "RENDERING", "RENDERED",
    "READY_TO_PUBLISH", "PUBLISHING", "PUBLISHED", "FAILED", "CANCELED",
    name="pipeline_state_enum",
    create_type=False,
)
PIPELINE_EVENT_TYPE = sa.Enum(
    "STATE_CHANGED", "INFO", "WARNING", "ERROR", "RETRY", "HEARTBEAT",
    name="pipeline_event_type_enum",
    create_type=False,
)
VIDEO_CANDIDATE_STATUS = sa.Enum(
    "DISCOVERED", "RANKED", "SELECTED", "REJECTED", "CONSUMED",
    name="video_candidate_status_enum",
    create_type=False,
)
DISCOVERY_SOURCE_KIND = sa.Enum(
    "YOUTUBE_TRENDING", "YOUTUBE_SEARCH", "NEWS", "RSS", "MANUAL",
    name="discovery_source_kind_enum",
    create_type=False,
)
GENERATED_ASSET_KIND = sa.Enum(
    "CLIP", "FINAL_VIDEO", "THUMBNAIL", "THUMBNAIL_PROMPT", "TITLE", "DESCRIPTION",
    "HASHTAGS", "SUBTITLES", "METADATA",
    name="generated_asset_kind_enum",
    create_type=False,
)
PUBLISH_PLATFORM = sa.Enum(
    "TELEGRAM", "YOUTUBE", "TIKTOK", "INSTAGRAM",
    name="publish_platform_enum",
    create_type=False,
)
PUBLISH_ATTEMPT_STATUS = sa.Enum(
    "PENDING", "IN_PROGRESS", "SUCCEEDED", "FAILED", "CANCELED",
    name="publish_attempt_status_enum",
    create_type=False,
)
CONNECTED_NODE_STATUS = sa.Enum(
    "ONLINE", "OFFLINE", "DEGRADED", "UNKNOWN",
    name="connected_node_status_enum",
    create_type=False,
)
AI_EXECUTION_STATUS = sa.Enum(
    "PENDING", "RUNNING", "SUCCEEDED", "FAILED",
    name="ai_execution_status_enum",
    create_type=False,
)

_ALL_ENUMS = [
    PIPELINE_STATE, PIPELINE_EVENT_TYPE, VIDEO_CANDIDATE_STATUS, DISCOVERY_SOURCE_KIND,
    GENERATED_ASSET_KIND, PUBLISH_PLATFORM, PUBLISH_ATTEMPT_STATUS, CONNECTED_NODE_STATUS,
    AI_EXECUTION_STATUS,
]


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    bind = op.get_bind()
    for enum in _ALL_ENUMS:
        enum.create(bind, checkfirst=True)

    # ---- content_topics ----
    op.create_table(
        "content_topics",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("keywords_json", _jsonb(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("schedule_hours_json", _jsonb(), nullable=True),
        sa.Column("cooldown_sec", sa.Integer(), nullable=False),
        sa.Column("max_daily_jobs", sa.Integer(), nullable=False),
        sa.Column("default_clip_mode", sa.String(length=64), nullable=False),
        sa.Column("default_video_ratio", sa.String(length=32), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", _jsonb(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_content_topics_name"),
    )

    # ---- discovery_sources ----
    op.create_table(
        "discovery_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", DISCOVERY_SOURCE_KIND, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("config_json", _jsonb(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["topic_id"], ["content_topics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_discovery_sources_topic_id"), "discovery_sources", ["topic_id"], unique=False)
    op.create_index("ix_discovery_sources_topic", "discovery_sources", ["topic_id"], unique=False)

    # ---- video_candidates ----
    op.create_table(
        "video_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("channel", sa.String(length=255), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedup_hash", sa.String(length=128), nullable=True),
        sa.Column("status", VIDEO_CANDIDATE_STATUS, nullable=False),
        sa.Column("relevance_score", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("trend_score", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("duplicate_score", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("quality_score", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("scores_json", _jsonb(), nullable=True),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", _jsonb(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["topic_id"], ["content_topics.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["discovery_sources.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_video_candidates_topic_id"), "video_candidates", ["topic_id"], unique=False)
    op.create_index("ix_video_candidates_topic_status", "video_candidates", ["topic_id", "status"], unique=False)
    op.create_index("ix_video_candidates_dedup", "video_candidates", ["dedup_hash"], unique=False)

    # ---- pipeline_jobs ----
    op.create_table(
        "pipeline_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_storage_key", sa.String(length=500), nullable=True),
        sa.Column("preset_id", sa.String(length=64), nullable=True),
        sa.Column("clip_mode", sa.String(length=64), nullable=False),
        sa.Column("video_ratio", sa.String(length=32), nullable=False),
        sa.Column("state", PIPELINE_STATE, nullable=False),
        sa.Column("pipeline_stage", sa.String(length=64), nullable=False),
        sa.Column("worker_job_id", sa.String(length=255), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", _jsonb(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["topic_id"], ["content_topics.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["candidate_id"], ["video_candidates.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pipeline_jobs_topic_id"), "pipeline_jobs", ["topic_id"], unique=False)
    op.create_index("ix_pipeline_jobs_state", "pipeline_jobs", ["state"], unique=False)
    op.create_index("ix_pipeline_jobs_topic_state", "pipeline_jobs", ["topic_id", "state"], unique=False)

    # ---- publish_targets ----
    op.create_table(
        "publish_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", PUBLISH_PLATFORM, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("account_ref", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("config_json", _jsonb(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # ---- pipeline_events ----
    op.create_table(
        "pipeline_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pipeline_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("service", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("event_type", PIPELINE_EVENT_TYPE, nullable=False),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column("payload_json", _jsonb(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["pipeline_job_id"], ["pipeline_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pipeline_events_pipeline_job_id"), "pipeline_events", ["pipeline_job_id"], unique=False)
    op.create_index("ix_pipeline_events_job_created", "pipeline_events", ["pipeline_job_id", "created_at"], unique=False)
    op.create_index("ix_pipeline_events_service_created", "pipeline_events", ["service", "created_at"], unique=False)
    op.create_index("ix_pipeline_events_type", "pipeline_events", ["event_type"], unique=False)

    # ---- generated_assets ----
    op.create_table(
        "generated_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pipeline_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", GENERATED_ASSET_KIND, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("storage_key", sa.String(length=500), nullable=True),
        sa.Column("public_url", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("metadata_json", _jsonb(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["pipeline_job_id"], ["pipeline_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_generated_assets_pipeline_job_id"), "generated_assets", ["pipeline_job_id"], unique=False)
    op.create_index("ix_generated_assets_job_kind", "generated_assets", ["pipeline_job_id", "kind"], unique=False)

    # ---- ai_executions ----
    op.create_table(
        "ai_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pipeline_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("purpose", sa.String(length=64), nullable=True),
        sa.Column("status", AI_EXECUTION_STATUS, nullable=False),
        sa.Column("prompt_chars", sa.Integer(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("payload_json", _jsonb(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["pipeline_job_id"], ["pipeline_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ai_executions_pipeline_job_id"), "ai_executions", ["pipeline_job_id"], unique=False)
    op.create_index("ix_ai_executions_job", "ai_executions", ["pipeline_job_id"], unique=False)

    # ---- publish_attempts ----
    op.create_table(
        "publish_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pipeline_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", PUBLISH_ATTEMPT_STATUS, nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("payload_json", _jsonb(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["pipeline_job_id"], ["pipeline_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["publish_targets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_publish_attempts_pipeline_job_id"), "publish_attempts", ["pipeline_job_id"], unique=False)
    op.create_index(op.f("ix_publish_attempts_target_id"), "publish_attempts", ["target_id"], unique=False)
    op.create_index("ix_publish_attempts_job", "publish_attempts", ["pipeline_job_id"], unique=False)

    # ---- connected_nodes ----
    op.create_table(
        "connected_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_id", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", CONNECTED_NODE_STATUS, nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("capabilities_json", _jsonb(), nullable=True),
        sa.Column("metadata_json", _jsonb(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", name="uq_connected_nodes_node_id"),
    )


def downgrade() -> None:
    op.drop_table("connected_nodes")

    op.drop_index("ix_publish_attempts_job", table_name="publish_attempts")
    op.drop_index(op.f("ix_publish_attempts_target_id"), table_name="publish_attempts")
    op.drop_index(op.f("ix_publish_attempts_pipeline_job_id"), table_name="publish_attempts")
    op.drop_table("publish_attempts")

    op.drop_index("ix_ai_executions_job", table_name="ai_executions")
    op.drop_index(op.f("ix_ai_executions_pipeline_job_id"), table_name="ai_executions")
    op.drop_table("ai_executions")

    op.drop_index("ix_generated_assets_job_kind", table_name="generated_assets")
    op.drop_index(op.f("ix_generated_assets_pipeline_job_id"), table_name="generated_assets")
    op.drop_table("generated_assets")

    op.drop_index("ix_pipeline_events_type", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_service_created", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_job_created", table_name="pipeline_events")
    op.drop_index(op.f("ix_pipeline_events_pipeline_job_id"), table_name="pipeline_events")
    op.drop_table("pipeline_events")

    op.drop_table("publish_targets")

    op.drop_index("ix_pipeline_jobs_topic_state", table_name="pipeline_jobs")
    op.drop_index("ix_pipeline_jobs_state", table_name="pipeline_jobs")
    op.drop_index(op.f("ix_pipeline_jobs_topic_id"), table_name="pipeline_jobs")
    op.drop_table("pipeline_jobs")

    op.drop_index("ix_video_candidates_dedup", table_name="video_candidates")
    op.drop_index("ix_video_candidates_topic_status", table_name="video_candidates")
    op.drop_index(op.f("ix_video_candidates_topic_id"), table_name="video_candidates")
    op.drop_table("video_candidates")

    op.drop_index("ix_discovery_sources_topic", table_name="discovery_sources")
    op.drop_index(op.f("ix_discovery_sources_topic_id"), table_name="discovery_sources")
    op.drop_table("discovery_sources")

    op.drop_table("content_topics")

    bind = op.get_bind()
    for enum in _ALL_ENUMS:
        enum.drop(bind, checkfirst=True)
