from datetime import datetime

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.user import User
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