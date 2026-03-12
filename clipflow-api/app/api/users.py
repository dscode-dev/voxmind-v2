from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.security.auth_middleware import get_current_user
from app.models.user import User

router = APIRouter()


@router.get("/me")
def get_me(user: User = Depends(get_current_user)):

    return {
        "id": str(user.id),
        "phone_number": user.phone_number,
        "full_name": user.full_name,
        "credits": user.credits,
        "status": user.status.name,
    }