from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.security.auth_middleware import get_current_user
from app.security.access_control import can_bypass_credits
from app.models.user import User
from app.core.settings import settings

router = APIRouter()


@router.get("/me")
def get_me(user: User = Depends(get_current_user)):

    return {
        "id": str(user.id),
        "phone_number": user.phone_number,
        "full_name": user.full_name,
        "credits": settings.default_admin_credits if can_bypass_credits(user) else user.credits,
        "status": user.status.value,
        "role": user.role.value,
        "is_admin": user.role.value == "admin",
    }
