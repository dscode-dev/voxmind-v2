"""Who may see what.

This deployment has one operator. The role column survives because the bootstrap sets it and
every guarded endpoint reads it, but there is no second kind of account any more: registration
is closed and `/auth/start` refuses a number nobody provisioned. What is left here is the
narrow question each endpoint actually asks.
"""
from sqlalchemy.orm import Query

from app.models.enums import UserRole
from app.models.user import User


def is_admin(user: User) -> bool:
    return user.role == UserRole.ADMIN


def scope_job_query(query: Query, user: User, model) -> Query:
    """Restrict a job query to what this account owns.

    Kept rather than removed: it is the one place that decides the answer, and a future second
    account would otherwise silently see everything.
    """
    if is_admin(user):
        return query
    return query.filter(model.user_id == user.id)
