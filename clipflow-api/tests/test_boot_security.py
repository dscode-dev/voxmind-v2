"""Security regressions closed by PR-BOOT-01.

Each test pins one behaviour that was previously fail-open:
  * a placeholder or missing secret must stop the API rather than sign tokens anyway;
  * a missing internal token must reject internal calls rather than disable authentication;
  * a fixed OTP must never be reachable without an explicit development environment.
"""

import os
from datetime import datetime, timedelta, timezone
from unittest import mock

import jwt
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.settings import Settings, settings
from app.security.auth_middleware import require_internal_api_token
from app.security.jwt_service import decode_token, generate_token


VALID_JWT_SECRET = "a-real-jwt-secret-0123456789"
VALID_INTERNAL_TOKEN = "a-real-internal-token-0123456789"

BASE_ENV = {
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
    "MINIO_ENDPOINT": "minio:9000",
    "MINIO_ACCESS_KEY": "access",
    "MINIO_SECRET_KEY": "secret",
}


def build_settings(**overrides) -> Settings:
    """Build Settings from explicit values only.

    The ambient environment is cleared for the duration of the call: conftest populates the
    required variables so the app can be imported, and without this a test that omits a
    field would silently pick that value back up from os.environ instead of exercising the
    "not configured" path.
    """
    values = {
        **BASE_ENV,
        "JWT_SECRET": VALID_JWT_SECRET,
        "INTERNAL_API_TOKEN": VALID_INTERNAL_TOKEN,
        **overrides,
    }
    values = {key: value for key, value in values.items() if value is not _UNSET}
    with mock.patch.dict(os.environ, {}, clear=True):
        return Settings(_env_file=None, **values)


class _Unset:
    pass


_UNSET = _Unset()


# ==========================================================================
# JWT — no fallback secret, central configuration honoured
# ==========================================================================


def test_jwt_secret_is_required():
    with pytest.raises(ValidationError):
        build_settings(JWT_SECRET=_UNSET)


def test_jwt_secret_rejects_the_old_hardcoded_fallback():
    # `clipflow-secret` was the committed fallback in jwt_service. It must not be accepted.
    with pytest.raises(ValidationError) as exc:
        build_settings(JWT_SECRET="clipflow-secret")
    assert "placeholder" in str(exc.value)


def test_jwt_secret_rejects_env_template_placeholder():
    with pytest.raises(ValidationError):
        build_settings(JWT_SECRET="change_me_jwt_secret")


def test_jwt_secret_rejects_short_values():
    with pytest.raises(ValidationError) as exc:
        build_settings(JWT_SECRET="short")
    assert "16 characters" in str(exc.value)


def test_jwt_round_trip_uses_configured_secret_and_algorithm():
    token = generate_token("user-123", 7, "fingerprint-abc")
    payload = decode_token(token)

    assert payload["sub"] == "user-123"
    assert payload["tv"] == 7
    assert payload["fp"] == "fingerprint-abc"

    # Signed with the configured secret, not a fallback.
    decoded = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert decoded["sub"] == "user-123"


def test_jwt_cannot_be_verified_with_the_old_fallback_secret():
    token = generate_token("user-123", 1, "fp")
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "clipflow-secret", algorithms=[settings.jwt_algorithm])


def test_jwt_expiration_honours_settings():
    token = generate_token("user-123", 1, "fp")
    payload = decode_token(token)

    expected = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_expiration_minutes
    )
    actual = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    assert abs((actual - expected).total_seconds()) < 60


# ==========================================================================
# Internal API token — fail closed
# ==========================================================================


def test_internal_token_is_required_configuration():
    with pytest.raises(ValidationError):
        build_settings(INTERNAL_API_TOKEN=_UNSET)


def test_internal_token_rejects_env_template_placeholder():
    with pytest.raises(ValidationError):
        build_settings(INTERNAL_API_TOKEN="change_me_internal_token")


def test_internal_guard_accepts_the_configured_token():
    assert require_internal_api_token(settings.internal_api_token) is None


def test_internal_guard_rejects_a_wrong_token():
    with pytest.raises(HTTPException) as exc:
        require_internal_api_token("not-the-configured-token")
    assert exc.value.status_code == 401


def test_internal_guard_rejects_a_missing_header():
    with pytest.raises(HTTPException) as exc:
        require_internal_api_token(None)
    assert exc.value.status_code == 401


def test_internal_guard_fails_closed_when_token_is_not_configured(monkeypatch):
    """The regression: an unconfigured token used to return early and authorize everyone."""
    monkeypatch.setattr(settings, "internal_api_token", "", raising=False)

    with pytest.raises(HTTPException) as exc:
        require_internal_api_token(None)
    assert exc.value.status_code == 401

    # Not even an empty header matches an empty configured token.
    with pytest.raises(HTTPException) as exc:
        require_internal_api_token("")
    assert exc.value.status_code == 401


# ==========================================================================
# Fixed OTP — development only, never a default
# ==========================================================================


def test_fixed_otp_has_no_default():
    """The regression: FIXED_TEST_OTP used to default to "123456"."""
    production = build_settings(ENVIRONMENT="production")
    assert production.fixed_test_otp is None
    assert production.resolve_fixed_otp() is None


def test_fixed_otp_has_no_default_when_environment_is_unset():
    unset_environment = build_settings()
    assert unset_environment.is_development is False
    assert unset_environment.resolve_fixed_otp() is None


def test_fixed_otp_in_production_is_a_startup_failure():
    with pytest.raises(ValidationError) as exc:
        build_settings(ENVIRONMENT="production", FIXED_TEST_OTP="123456")
    assert "FIXED_TEST_OTP" in str(exc.value)


def test_fixed_otp_requires_an_explicit_development_environment():
    development = build_settings(ENVIRONMENT="development", FIXED_TEST_OTP="123456")
    assert development.is_development is True
    assert development.resolve_fixed_otp() == "123456"


def test_development_without_fixed_otp_still_generates_a_random_code():
    development = build_settings(ENVIRONMENT="development")
    assert development.resolve_fixed_otp() is None


@pytest.mark.parametrize("environment", ["Production", "staging", "prod", "", "anything"])
def test_unrecognised_environments_are_treated_as_production(environment):
    assert build_settings(ENVIRONMENT=environment).is_development is False


# ==========================================================================
# MinIO — internal vs public endpoint
# ==========================================================================


def test_public_endpoint_defaults_to_internal_when_unset():
    configured = build_settings()
    assert configured.resolved_minio_public_endpoint == "minio:9000"


def test_public_endpoint_is_used_for_presigning_when_set():
    configured = build_settings(
        MINIO_PUBLIC_ENDPOINT="clipflow.example.com:9000",
        MINIO_PUBLIC_SECURE=True,
    )
    assert configured.minio_endpoint == "minio:9000"
    assert configured.resolved_minio_public_endpoint == "clipflow.example.com:9000"
    assert configured.resolved_minio_public_secure is True


def test_public_secure_falls_back_to_internal_secure():
    configured = build_settings(MINIO_SECURE=True)
    assert configured.resolved_minio_public_secure is True
