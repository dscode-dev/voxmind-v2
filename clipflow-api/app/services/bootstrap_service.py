from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.security.phone import normalize_phone_number


class BootstrapService:

    def ensure_default_admin(self, db: Session) -> None:
        try:
            phone_number = normalize_phone_number(settings.default_admin_phone_number, "BR")
        except ValueError:
            return
        if not phone_number:
            return

        user = db.query(User).filter(User.phone_number == phone_number).first()
        if user is None:
            user = User(
                phone_number=phone_number,
                full_name=settings.default_admin_full_name,
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
                credits=settings.default_admin_credits,
                token_version=1,
            )
            db.add(user)
        else:
            user.role = UserRole.ADMIN
            user.status = UserStatus.ACTIVE
            if not user.full_name:
                user.full_name = settings.default_admin_full_name
            if user.credits < settings.default_admin_credits:
                user.credits = settings.default_admin_credits

        db.commit()
