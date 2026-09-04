"""What a run is *required* to publish, decided once and never re-derived.

**The distinction this exists to make.** Until now the system could only ask "which
PublishAttempts exist?", and answered "is this run published?" with "did all of them
succeed?". Those are different questions. A run with four clips and two attempts, both
succeeded, satisfied the second and failed the first — and was marked PUBLISHED with two
videos never uploaded and nothing left to say so.

So the required set is established separately from the attempts, and before any of them
exist.

**Source of truth: publish_package.json, snapshotted.** The package's ``videos[]`` is already
the contract for "one publishable output" — each entry carries its own title, description and
rendered clip. It is written once by the worker at finalize. But reading it on every
completion check would mean a MinIO round trip in the publisher's hot path, and would leave
the definition of done depending on an object that is mutable in principle: a re-render
overwrites the same key, and the set of things a run must publish would silently change
underneath decisions already taken.

The manifest is therefore built once — on first access, which is before any attempt can exist
— and persisted onto the run. From then on it is read from the row, and a later re-render
cannot redefine what this run was supposed to do.

**Only generated outputs are required.** ``final_clip.status`` is ``generated`` or
``missing``; a clip that never rendered is not something to wait for, and treating it as
required would leave every such run permanently incomplete.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.pipeline_job import PipelineJob
from app.models.publish_attempt import PublishAttempt

logger = logging.getLogger(__name__)

MANIFEST_KEY = "publication_manifest"
MANIFEST_VERSION = 1

# A manifest reconstructed from the attempts of a run that predates this feature. Kept
# distinct from version 1 so the compatibility path is visible in the data rather than
# inferred, and so a legacy run can never be mistaken for one whose required set was
# genuinely established up front.
LEGACY_VERSION = 0

SOURCE_PACKAGE = "publish_package"
SOURCE_LEGACY_ATTEMPTS = "legacy_attempts"

# The only value of ``final_clip.status`` that means "this file exists and is publishable".
GENERATED = "generated"


class ManifestUnavailableError(RuntimeError):
    """The required set cannot be established, so nothing may be concluded about the run.

    Fail-closed and deliberately not "zero required items": an empty manifest would read as
    "everything required has been published", which is the precise mistake this module was
    written to prevent.
    """


@dataclass(frozen=True)
class RequiredItem:
    """One external publication this run owes."""

    media_identity: str
    video_index: int
    file_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "media_identity": self.media_identity,
            "video_index": self.video_index,
            "file_name": self.file_name,
            # Every item in the manifest is required by construction; the field is written
            # so a future optional output has somewhere to say so without a version bump.
            "required": True,
        }


@dataclass(frozen=True)
class PublicationManifest:
    version: int
    source: str
    items: tuple[RequiredItem, ...]
    created_at: str

    @property
    def is_legacy(self) -> bool:
        return self.version == LEGACY_VERSION

    def identities(self) -> list[str]:
        """Deterministic order: the package's own, by video_index ascending.

        Never attempt id or insertion order — a series must publish first clip first, and an
        order that depends on a UUID is not an order anyone can predict or reproduce.
        """
        return [item.media_identity for item in self.ordered()]

    def ordered(self) -> list[RequiredItem]:
        return sorted(self.items, key=lambda item: (item.video_index, item.media_identity))

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "created_at": self.created_at,
            "items": [item.as_dict() for item in self.ordered()],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PublicationManifest":
        items = tuple(
            RequiredItem(
                media_identity=str(entry.get("media_identity")),
                video_index=int(entry.get("video_index") or 0),
                file_name=str(entry.get("file_name") or ""),
            )
            for entry in (raw.get("items") or [])
            if entry.get("media_identity")
        )
        return cls(
            version=int(raw.get("version") or MANIFEST_VERSION),
            source=str(raw.get("source") or SOURCE_PACKAGE),
            items=items,
            created_at=str(raw.get("created_at") or ""),
        )


class ManifestService:
    """Builds, persists and reads the required publication set for a run."""

    def __init__(self, artifacts=None) -> None:
        if artifacts is None:
            from app.services.artifact_content_service import ArtifactContentService

            artifacts = ArtifactContentService()
        self.artifacts = artifacts

    # -------------------------------------------------------------------- read

    def load(self, job: PipelineJob) -> PublicationManifest | None:
        raw = (job.metadata_json or {}).get(MANIFEST_KEY)
        if not isinstance(raw, dict) or not raw.get("items"):
            return None
        return PublicationManifest.from_dict(raw)

    def resolve(self, db: Session, job: PipelineJob) -> PublicationManifest:
        """The run's required set, establishing it on first access.

        Lazy rather than written by the state machine, because building it needs the artifact
        store and the state machine has no business reaching into MinIO. First access always
        happens before any attempt exists — the publication path asks for it to decide what to
        create — so "on first access" and "before anything was published" are the same moment.
        """
        existing = self.load(job)
        if existing is not None:
            return existing

        manifest = self._build(db, job)
        self._persist(db, job, manifest)
        logger.info(
            "publication_manifest_created",
            extra={
                "pipeline_job_id": str(job.id),
                "manifest_version": manifest.version,
                "manifest_source": manifest.source,
                "required_count": len(manifest.items),
            },
        )
        return manifest

    # ------------------------------------------------------------------ build

    def _build(self, db: Session, job: PipelineJob) -> PublicationManifest:
        package = self._package(job)
        if package:
            items = self._items_from_package(package)
            if items:
                return PublicationManifest(
                    version=MANIFEST_VERSION,
                    source=SOURCE_PACKAGE,
                    items=tuple(items),
                    created_at=_now(),
                )

        # The package is unreadable or declares nothing publishable. If the run already has
        # publications, it predates this feature: its required set is reconstructed from what
        # was actually attempted, which reproduces exactly the behaviour those rows were
        # created under. That is a compatibility path, and it is labelled as one.
        legacy = self._items_from_attempts(db, job)
        if legacy:
            logger.info(
                "publication_manifest_legacy",
                extra={"pipeline_job_id": str(job.id), "required_count": len(legacy)},
            )
            return PublicationManifest(
                version=LEGACY_VERSION,
                source=SOURCE_LEGACY_ATTEMPTS,
                items=tuple(legacy),
                created_at=_now(),
            )

        # Nothing to go on. Refused rather than treated as "no outputs required", because
        # that would make an unreadable artifact look like a completed publication.
        raise ManifestUnavailableError(
            f"publish_package.json is not readable for job {job.worker_job_id} and the run "
            "has no publications to reconstruct from"
        )

    def _items_from_package(self, package: dict[str, Any]) -> list[RequiredItem]:
        items: list[RequiredItem] = []
        for index, video in enumerate(package.get("videos") or [], start=1):
            clip = video.get("final_clip") or {}
            file_name = clip.get("file_name")
            # Only outputs that actually rendered. A clip whose status is "missing" was never
            # produced, and requiring it would leave the run incomplete for ever.
            if not file_name or clip.get("status") != GENERATED:
                continue
            items.append(
                RequiredItem(
                    media_identity=f"final_clips/{file_name}",
                    video_index=int(video.get("video_index") or index),
                    file_name=str(file_name),
                )
            )
        return items

    @staticmethod
    def _items_from_attempts(db: Session, job: PipelineJob) -> list[RequiredItem]:
        rows = (
            db.query(PublishAttempt.media_identity, PublishAttempt.payload_json)
            .filter(PublishAttempt.pipeline_job_id == job.id)
            .all()
        )
        seen: dict[str, RequiredItem] = {}
        for identity, payload in rows:
            if not identity or identity in seen:
                continue
            index = int(((payload or {}).get("video_index")) or len(seen) + 1)
            seen[identity] = RequiredItem(
                media_identity=identity,
                video_index=index,
                file_name=identity.rsplit("/", 1)[-1],
            )
        return list(seen.values())

    # ---------------------------------------------------------------- persist

    @staticmethod
    def _persist(db: Session, job: PipelineJob, manifest: PublicationManifest) -> None:
        """Written once. Never rewritten.

        ``resolve`` returns early when one exists, so this is only reached for a run that has
        none — which is what makes the required set stable for the life of the run. A genuine
        re-production is a new run, not a quiet edit to this one's definition of done.
        """
        metadata = dict(job.metadata_json or {})
        metadata[MANIFEST_KEY] = manifest.as_dict()
        job.metadata_json = metadata
        db.flush()

    def _package(self, job: PipelineJob) -> dict[str, Any]:
        try:
            data = self.artifacts.load_json(
                f"jobs/{job.worker_job_id}/publish_package.json"
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "publication_manifest_package_unreadable",
                extra={"pipeline_job_id": str(job.id)},
            )
            return {}
        return data if isinstance(data, dict) else {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
