import secrets
from datetime import datetime

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.user import User
from app.core.settings import settings
from app.security.access_control import is_admin
from app.security.jwt_service import decode_token, _fingerprint


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:

    token = request.cookies.get("cf_session")

    if not token:
        raise HTTPException(status_code=401, detail="Missing session")

    try:

        payload = decode_token(token)

    except Exception:

        raise HTTPException(status_code=401, detail="Invalid session")

    user_id = payload.get("sub")
    token_version = payload.get("tv")
    fingerprint = payload.get("fp")

    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    if user.token_version != token_version:
        raise HTTPException(status_code=401, detail="Token expired")

    if user.status.name != "ACTIVE":
        raise HTTPException(status_code=403, detail="User disabled")

    # fingerprint verification
    current_fp = _fingerprint(request)

    if fingerprint != current_fp:
        raise HTTPException(status_code=401, detail="Session mismatch")

    user.last_seen_at = datetime.utcnow()

    db.commit()

    return user


def get_current_admin(
    user: User = Depends(get_current_user),
) -> User:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_internal_api_token(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """Fail-closed guard for /internal/* endpoints.

    A missing or empty INTERNAL_API_TOKEN rejects every call rather than disabling
    authentication. Settings already refuses to start without one; this is defence in depth
    for any path that constructs Settings differently.
    """
    expected = str(settings.internal_api_token or "").strip()
    if not expected:
        raise HTTPException(
            status_code=401,
            detail="Internal API authentication is not configured",
        )
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status_code=401, detail="Invalid internal token")
