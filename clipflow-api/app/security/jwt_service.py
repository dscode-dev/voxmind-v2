import hashlib
import os
from datetime import datetime, timedelta
from typing import Any, Dict

import jwt
from fastapi import Request


JWT_SECRET = os.getenv("JWT_SECRET", "clipflow-secret")
JWT_ALGORITHM = "HS256"
JWT_EXP_DAYS = 30


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

    payload: Dict[str, Any] = {
        "sub": str(user_id),
        "tv": token_version,
        "fp": fingerprint,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(days=JWT_EXP_DAYS),
    }

    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Dict[str, Any]:

    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])