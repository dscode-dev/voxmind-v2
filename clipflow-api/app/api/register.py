from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


router = APIRouter()


class RegisterInput(BaseModel):

    phone_number: str
    full_name: str | None = None


class RegisterResponse(BaseModel):
    detail: str


@router.post("/register", response_model=RegisterResponse)
def register(_: RegisterInput):
    raise HTTPException(
        status_code=410,
        detail="Deprecated endpoint. Use /auth/start and /auth/verify.",
    )
