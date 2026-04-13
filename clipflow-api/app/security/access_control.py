from sqlalchemy.orm import Query

from app.core.settings import settings
from app.models.user import User
from app.models.enums import UserRole
from app.security.phone import normalize_phone_number


def is_admin(user: User) -> bool:
    return user.role == UserRole.ADMIN


def is_lab_unlimited_credit_user(user: User) -> bool:
    configured_numbers = [
        item.strip()
        for item in str(settings.lab_unlimited_credit_phone_numbers or "").split(",")
        if item.strip()
    ]
    configured_numbers.append(settings.default_admin_phone_number)

    normalized_numbers: set[str] = set()
    for number in configured_numbers:
        try:
            normalized_numbers.add(normalize_phone_number(number, "BR"))
        except ValueError:
            normalized_numbers.add(number)

    return user.phone_number in normalized_numbers


def can_bypass_credits(user: User) -> bool:
    return is_admin(user) or is_lab_unlimited_credit_user(user)


def scope_job_query(query: Query, user: User, model) -> Query:
    if is_admin(user):
        return query
    return query.filter(model.user_id == user.id)
