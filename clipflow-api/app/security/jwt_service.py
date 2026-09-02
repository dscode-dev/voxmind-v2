import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict

import jwt
from fastapi import Request

from app.core.settings import settings


def _fingerprint(request: Request) -> str:
    """
    Gera fingerprint do cliente
    """

    user_agent = request.headers.get("user-agent", "")

    ip = request.client.host if request.client else "0.0.0.0"

    ip_prefix = ".".join(ip.split(".")[:3])

    raw = f"{user_agent}:{ip_prefix}"

    return hashlib.sha256(raw.encode()).hexdigest()


def generate_token(user_id: str, token_version: int, fingerprint: str) -> str:

    now = datetime.utcnow()

    payload: Dict[str, Any] = {
        "sub": str(user_id),
        "tv": token_version,
        "fp": fingerprint,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expiration_minutes),
    }

    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> Dict[str, Any]:

    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
