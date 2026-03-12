from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.user import User
from app.security.jwt_service import _fingerprint, generate_token


router = APIRouter()


class RegisterInput(BaseModel):

    phone_number: str
    full_name: str | None = None


class RegisterResponse(BaseModel):

    user_id: str
    credits: int


@router.post("/register", response_model=RegisterResponse)
def register(
    payload: RegisterInput,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):

    user = db.query(User).filter(
        User.phone_number == payload.phone_number
    ).first()

    if not user:

        user = User(
            phone_number=payload.phone_number,
            full_name=payload.full_name,
            credits=0,
            token_version=1,
        )

        db.add(user)
        db.commit()
        db.refresh(user)

    fp = _fingerprint(request)

    user.fingerprint_hash = fp
    user.token_created_at = datetime.utcnow()

    token = generate_token(
        user_id=str(user.id),
        token_version=user.token_version,
        fingerprint=fp,
    )

    db.commit()

    response.set_cookie(
        key="cf_session",
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )

    return RegisterResponse(
        user_id=str(user.id),
        credits=user.credits,
    )