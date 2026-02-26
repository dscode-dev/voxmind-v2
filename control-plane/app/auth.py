from fastapi import Header, HTTPException
from .settings import settings

def require_api_key(x_api_key: str | None = Header(default=None, alias="X-Api-Key")) -> None:
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
