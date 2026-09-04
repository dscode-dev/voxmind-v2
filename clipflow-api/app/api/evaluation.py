"""Reading the evaluation dataset. Admin-only, and read-only in the strictest sense.

Every route here resolves canonical windows over snapshots that already exist. None of them
calls YouTube, none writes a row, and none can influence what the system produces next.

**What is never returned**: a refresh token, an access token, an encrypted credential, an
upload session URI, or a raw provider error body. The read models are assembled from named
columns and frozen JSON keys chosen for that reason — there is no place for a secret to
arrive through, rather than a filter that has to remember to remove one.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.evaluation.schema import schema_contract
from app.evaluation.windows import policy_description
from app.models.publish_attempt import PublishAttempt
from app.models.user import User
from app.security.auth_middleware import get_current_admin
from app.services.performance_dataset_service import (
    DatasetFilters,
    PerformanceDatasetService,
)

router = APIRouter()

# A page of rows, not the whole dataset: an unbounded JSON response is how a browser tab dies
# on a channel with a year of publications. The CSV export is the path for everything.
MAX_PAGE = 500


def _service() -> PerformanceDatasetService:
    return PerformanceDatasetService()


def _filters(
    topic_id: str | None,
    published_from: datetime | None,
    published_to: datetime | None,
    initiator: str | None,
    privacy: str | None,
) -> DatasetFilters:
    return DatasetFilters(
        topic_id=topic_id,
        published_from=published_from,
        published_to=published_to,
        initiator=initiator,
        privacy=privacy,
    )


@router.get("/admin/published-videos/{attempt_id}/evaluation")
def video_evaluation(
    attempt_id: uuid.UUID,
    as_of: datetime | None = Query(
        None, description="Ignore snapshots captured after this instant (default: now)."
    ),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """One publication against the canonical windows.

    Returns the same resolution the dataset build uses, including the trace that says which
    snapshot supports each figure and how late it was. A publication that cannot be evaluated
    comes back with ``evaluable: false`` and the reason, rather than a row of nulls that would
    read as "measured, and it was nothing".
    """
    attempt = db.get(PublishAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="publication not found"
        )
    return _service().evaluate_one(db, attempt, as_of=as_of)


@router.post("/admin/metrics/evaluation-datasets")
def build_evaluation_dataset(
    dry_run: bool = Query(
        True,
        description="Return the manifest, summary and data quality without any rows.",
    ),
    as_of: datetime | None = Query(None),
    topic_id: str | None = Query(None),
    published_from: datetime | None = Query(None),
    published_to: datetime | None = Query(None),
    initiator: str | None = Query(None),
    privacy: str | None = Query(None),
    limit: int = Query(100, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Build a dataset and describe it.

    ``dry_run`` defaults to **true**, which here means "tell me what this dataset would
    contain" — how many publications were considered, how many became rows, which windows are
    mature, and where the coverage holes are. That is the question worth asking first: a
    dataset whose 24h coverage is 30% is not one to start analysing.

    Nothing is persisted either way. Rows are derived from immutable snapshots, so the
    manifest plus ``as_of`` is enough to rebuild this exact dataset later — a stored copy
    would only add a second truth that can disagree with the series it came from.
    """
    dataset = _service().build(
        db,
        as_of=as_of,
        filters=_filters(topic_id, published_from, published_to, initiator, privacy),
    )
    payload = dataset.as_dict()
    if dry_run:
        return payload

    page = dataset.rows[offset:offset + limit]
    payload["rows"] = [row.as_dict() for row in page]
    payload["page"] = {
        "limit": limit,
        "offset": offset,
        "returned": len(page),
        "total": len(dataset.rows),
    }
    return payload


@router.get("/admin/metrics/evaluation-datasets/{dataset_id}")
def read_evaluation_dataset(
    dataset_id: str,
    as_of: datetime | None = Query(None),
    topic_id: str | None = Query(None),
    published_from: datetime | None = Query(None),
    published_to: datetime | None = Query(None),
    initiator: str | None = Query(None),
    privacy: str | None = Query(None),
    limit: int = Query(100, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Read back a dataset by id, rebuilding it from the same inputs.

    The id is a digest of the semantic version, the window policy, ``as_of`` and the filters,
    so rebuilding with those parameters must reproduce it exactly. If it does not, the id
    will not match and this returns 404 rather than quietly handing back a different dataset
    under the name that was asked for — which is the failure mode versioning exists to catch.
    """
    dataset = _service().build(
        db,
        as_of=as_of,
        filters=_filters(topic_id, published_from, published_to, initiator, privacy),
    )
    if dataset.manifest.dataset_id != dataset_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "no dataset with that id for these parameters; "
                f"the given parameters build {dataset.manifest.dataset_id}"
            ),
        )
    page = dataset.rows[offset:offset + limit]
    payload = dataset.as_dict()
    payload["rows"] = [row.as_dict() for row in page]
    payload["page"] = {
        "limit": limit,
        "offset": offset,
        "returned": len(page),
        "total": len(dataset.rows),
    }
    return payload


@router.get("/admin/metrics/evaluation-datasets/{dataset_id}/export.csv")
def export_evaluation_dataset(
    dataset_id: str,
    as_of: datetime | None = Query(None),
    topic_id: str | None = Query(None),
    published_from: datetime | None = Query(None),
    published_to: datetime | None = Query(None),
    initiator: str | None = Query(None),
    privacy: str | None = Query(None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """The whole dataset as CSV, streamed.

    A dataset only becomes useful when it can be opened somewhere else — a notebook, a
    spreadsheet, R. The columns are declared and versioned rather than derived from whichever
    row serialized first, so two exports are diffable and a consumer can rely on the header.
    """
    service = _service()
    dataset = service.build(
        db,
        as_of=as_of,
        filters=_filters(topic_id, published_from, published_to, initiator, privacy),
    )
    if dataset.manifest.dataset_id != dataset_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "no dataset with that id for these parameters; "
                f"the given parameters build {dataset.manifest.dataset_id}"
            ),
        )
    filename = f"{dataset.manifest.semantic_version}-{dataset_id}.csv"
    return StreamingResponse(
        service.to_csv(dataset),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The manifest travels with the file, so a CSV found on a laptop months later can
            # still say which rules produced it.
            "X-Dataset-Id": dataset.manifest.dataset_id,
            "X-Dataset-Version": dataset.manifest.semantic_version,
            "X-Window-Policy-Version": dataset.manifest.window_policy_version,
            "X-Export-Schema-Version": dataset.manifest.export_schema_version,
            "X-Dataset-Row-Count": str(dataset.manifest.row_count),
        },
    )


@router.get("/admin/metrics/evaluation-schema")
def evaluation_schema(
    response: Response,
    admin: User = Depends(get_current_admin),
):
    """The window policy and the column contract, without building anything."""
    response.headers["Cache-Control"] = "no-store"
    return {"windows": policy_description(), "schema": schema_contract()}
