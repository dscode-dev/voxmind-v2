"""The one place that decides whether a fixed login code applies.

**Why this is a module and not an `if` in the endpoint.** The rule has four conditions and
exactly one of them being wrong turns a local convenience into a way into somebody's account.
Written once, it can be read once and tested directly; spread across the two auth endpoints
it becomes two rules that drift.

**Why it is scoped to one phone.** The mechanism it replaces resolved a fixed code for
*every* login, and `/auth/start` creates a user for an unknown number — so a fixed code was a
way to mint an account for any phone at all and sign in as it. This one answers "does this
specific phone have a fixed code", and the answer is no for every phone but the bootstrap
admin's.
"""
from __future__ import annotations

from app.core.settings import settings
from app.security.phone import normalize_phone_number


def bootstrap_admin_phone() -> str | None:
    """The bootstrap admin's number, in the same canonical form the login stores.

    Normalised here rather than compared raw: the same account written `+5581...`, `5581...`
    and `(81) ...` must be one identity, and the login path already canonicalises what it
    receives. Comparing an un-normalised env value would make the code work or not depending
    on how the number happened to be typed into `.env`.
    """
    configured = str(settings.default_admin_phone_number or "").strip()
    if not configured:
        return None
    try:
        normalized = normalize_phone_number(configured, "BR")
    except ValueError:
        return None
    return normalized or None


def resolve_bootstrap_code(phone_number: str | None) -> str | None:
    """The fixed code for this phone, or None.

    Returns a value only when every condition holds: the feature is enabled, the environment
    is a development one, a code is configured, and this is the bootstrap admin's number.
    The caller uses it as the OTP for that login instead of a generated one — so no branch
    anywhere says "if the code matches, authenticate": the code simply *is* the code for that
    account, and the ordinary verification path checks it like any other.
    """
    code = settings.resolve_bootstrap_auth_code()
    if not code:
        return None

    expected = bootstrap_admin_phone()
    if not expected or not phone_number:
        return None
    if phone_number != expected:
        return None
    return code


def is_bootstrap_admin(phone_number: str | None) -> bool:
    """Whether this number is the configured bootstrap admin, regardless of the feature flag."""
    expected = bootstrap_admin_phone()
    return bool(expected and phone_number and phone_number == expected)
