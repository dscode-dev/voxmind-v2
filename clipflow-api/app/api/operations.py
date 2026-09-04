"""The product's operational health, as distinct from the process's.

``/health`` and ``/ready`` answer "is this container working" — they are what Docker and any
orchestrator watch, and they must keep meaning exactly that. This answers "is the product
working": whether publications are stuck, whether the autonomous loop is ticking, whether a
channel has lost its credential.

The two are deliberately different endpoints with different consumers. A YouTube token
expiring is a real problem and a real signal here, and it must never make an orchestrator
restart a perfectly healthy API — which is what would happen if it leaked into ``/ready``.

For the same reason this returns **200 with a status field**, not a 5xx. The request
succeeded; the answer is that the product is degraded.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.user import User
from app.security.auth_middleware import get_current_admin
from app.services.operations_service import OperationsService

router = APIRouter()


def _operations() -> OperationsService:
    return OperationsService()


@router.get("/admin/operations/health")
def operations_health(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Every operational signal, active or not.

    Inactive signals are returned too, so a reader can tell "this condition was checked and
    is fine" from "this condition is not checked" — a list that only ever contains problems
    cannot distinguish the two.
    """
    return _operations().health(db)
