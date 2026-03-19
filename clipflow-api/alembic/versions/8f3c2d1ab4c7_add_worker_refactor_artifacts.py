"""add worker refactor artifacts

Revision ID: 8f3c2d1ab4c7
Revises: 009e9c12ac00
Create Date: 2026-03-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "8f3c2d1ab4c7"
down_revision = "009e9c12ac00"
branch_labels = None
depends_on = None


NEW_CLIP_ASSET_TYPES = [
    "TRANSCRIPT_WITH_SPEAKERS",
    "SPEAKER_TURNS",
    "CANDIDATES",
    "QA_REPORT",
    "DELIVERY_PACKAGE",
    "ARTIFACTS_MANIFEST",
    "RUNTIME_STATUS",
]


def upgrade() -> None:
    for asset_type in NEW_CLIP_ASSET_TYPES:
        op.execute(
            sa.text(
                f"ALTER TYPE clip_asset_type_enum ADD VALUE IF NOT EXISTS '{asset_type}'"
            )
        )

    op.add_column(
        "clip_jobs",
        sa.Column("transcript_with_speakers_storage_key", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "clip_jobs",
        sa.Column("speaker_turns_storage_key", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "clip_jobs",
        sa.Column("candidates_storage_key", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "clip_jobs",
        sa.Column("qa_report_storage_key", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "clip_jobs",
        sa.Column("delivery_package_storage_key", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "clip_jobs",
        sa.Column("artifacts_manifest_storage_key", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "clip_jobs",
        sa.Column("runtime_status_storage_key", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("clip_jobs", "runtime_status_storage_key")
    op.drop_column("clip_jobs", "artifacts_manifest_storage_key")
    op.drop_column("clip_jobs", "delivery_package_storage_key")
    op.drop_column("clip_jobs", "qa_report_storage_key")
    op.drop_column("clip_jobs", "candidates_storage_key")
    op.drop_column("clip_jobs", "speaker_turns_storage_key")
    op.drop_column("clip_jobs", "transcript_with_speakers_storage_key")
