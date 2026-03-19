from sqlalchemy.orm import Query

from app.models.user import User
from app.models.enums import UserRole


def is_admin(user: User) -> bool:
    return user.role == UserRole.ADMIN


def can_bypass_credits(user: User) -> bool:
    return is_admin(user)


def scope_job_query(query: Query, user: User, model) -> Query:
    if is_admin(user):
        return query
    return query.filter(model.user_id == user.id)
