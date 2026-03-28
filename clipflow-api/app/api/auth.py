from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.db.session import get_db
from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.security.auth_middleware import get_current_user
from app.security.phone import normalize_phone_number
from app.security.jwt_service import _fingerprint, generate_token
from app.services.audit_service import AuditService
from app.services.otp_service import (
    challenge_expiration,
    generate_challenge_id,
    generate_otp,
    hash_otp,
    otp_expiration,
    verify_otp,
)

router = APIRouter(prefix="/auth", tags=["auth"])
audit_service = AuditService()


# ==========================================
# Schemas
# ==========================================

class StartAuthInput(BaseModel):
    phone_number: str = Field(..., min_length=5, max_length=32)
    country_code: str = Field(..., min_length=1, max_length=10)
    full_name: str | None = Field(default=None, max_length=255)


class StartAuthResponse(BaseModel):
    status: str
    challenge_id: str
    expires_in_seconds: int


class VerifyAuthInput(BaseModel):
    phone_number: str = Field(..., min_length=5, max_length=32)
    code: str = Field(..., min_length=4, max_length=10)
    challenge_id: str = Field(..., min_length=8, max_length=128)
    remember_me: bool = False


class VerifyAuthResponse(BaseModel):
    status: str


class MeResponse(BaseModel):
    id: str
    full_name: str | None
    phone_number: str
    credits: int
    status: str
    role: str
    is_admin: bool


# ==========================================
# Start authentication (OTP request)
# ==========================================

@router.post("/start", response_model=StartAuthResponse)
def start_auth(
    payload: StartAuthInput,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        phone_number = normalize_phone_number(payload.phone_number, payload.country_code)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid phone number")

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    if ip_address and audit_service.recent_count(
        db,
        action="auth.start",
        since_seconds=settings.otp_rate_limit_window_sec,
        ip_address=ip_address,
    ) >= settings.otp_request_limit_per_ip_window:
        audit_service.log(
            db,
            action="auth.start",
            outcome="rate_limited",
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={"phone_number": phone_number, "country_code": payload.country_code},
        )
        db.commit()
        raise HTTPException(status_code=429, detail="Too many login attempts from this IP")

    if audit_service.recent_count(
        db,
        action="auth.start",
        since_seconds=settings.otp_rate_limit_window_sec,
        phone_number=phone_number,
    ) >= settings.otp_request_limit_per_phone_window:
        audit_service.log(
            db,
            action="auth.start",
            outcome="rate_limited",
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={"phone_number": phone_number, "country_code": payload.country_code},
        )
        db.commit()
        raise HTTPException(status_code=429, detail="Too many codes requested for this phone")

    user = (
        db.query(User)
        .filter(User.phone_number == phone_number)
        .first()
    )

    now = datetime.now(timezone.utc)

    if not user:
        user = User(
            phone_number=phone_number,
            full_name=payload.full_name,
            credits=0,
            token_version=1,
        )
        db.add(user)
        db.flush()
    else:
        if user.status in {UserStatus.SUSPENDED, UserStatus.DELETED}:
            raise HTTPException(status_code=403, detail="Account unavailable")
        if payload.full_name and not user.full_name:
            user.full_name = payload.full_name

    if user.otp_last_sent_at:
        delta = now - user.otp_last_sent_at
        if delta.total_seconds() < 30:
            raise HTTPException(
                status_code=429,
                detail="Please wait before requesting another code",
            )

    if user.otp_locked_until and user.otp_locked_until > now:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Try again later.",
        )

    code = str(settings.fixed_test_otp or "").strip() or generate_otp()
    challenge_id = generate_challenge_id()

    user.otp_hash = hash_otp(code)
    user.otp_expires_at = otp_expiration()
    user.otp_last_sent_at = now
    user.otp_attempts = 0
    user.otp_challenge_id = challenge_id
    user.otp_challenge_expires_at = challenge_expiration()

    db.commit()

    audit_service.log(
        db,
        action="auth.start",
        outcome="success",
        actor_user=user,
        target_type="user",
        target_id=str(user.id),
        ip_address=ip_address,
        user_agent=user_agent,
        metadata={"phone_number": phone_number, "country_code": payload.country_code},
    )
    db.commit()

    # TODO integrar provedor real de SMS
    print("OTP:", code)

    return StartAuthResponse(
        status="code_sent",
        challenge_id=challenge_id,
        expires_in_seconds=300,
    )


# ==========================================
# Verify OTP
# ==========================================

@router.post("/verify", response_model=VerifyAuthResponse)
def verify_code(
    payload: VerifyAuthInput,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    try:
        phone_number = normalize_phone_number(payload.phone_number)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid authentication flow")

    if ip_address and audit_service.recent_count(
        db,
        action="auth.verify",
        since_seconds=settings.otp_rate_limit_window_sec,
        ip_address=ip_address,
        outcome="failed",
    ) >= settings.otp_verify_fail_limit_per_ip_window:
        audit_service.log(
            db,
            action="auth.verify",
            outcome="rate_limited",
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={"phone_number": phone_number},
        )
        db.commit()
        raise HTTPException(status_code=429, detail="Too many failed verification attempts")

    user = (
        db.query(User)
        .filter(User.phone_number == phone_number)
        .first()
    )

    if not user:
        raise HTTPException(status_code=401, detail="Invalid authentication flow")

    if user.status in {UserStatus.SUSPENDED, UserStatus.DELETED}:
        raise HTTPException(status_code=403, detail="Account unavailable")

    now = datetime.now(timezone.utc)

    if user.otp_locked_until and user.otp_locked_until > now:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Try again later.",
        )

    if not user.otp_challenge_id or user.otp_challenge_id != payload.challenge_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid challenge",
        )

    if not user.otp_challenge_expires_at or user.otp_challenge_expires_at < now:
        raise HTTPException(
            status_code=401,
            detail="Challenge expired",
        )

    if not user.otp_expires_at or user.otp_expires_at < now:
        raise HTTPException(
            status_code=401,
            detail="Code expired",
        )

    if not verify_otp(payload.code, user.otp_hash):
        user.otp_attempts += 1

        if user.otp_attempts >= 5:
            user.otp_locked_until = now + timedelta(minutes=5)

        db.commit()
        audit_service.log(
            db,
            action="auth.verify",
            outcome="failed",
            actor_user=user,
            target_type="user",
            target_id=str(user.id),
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={"phone_number": phone_number},
        )
        db.commit()

        raise HTTPException(
            status_code=401,
            detail="Invalid code",
        )

    fingerprint = _fingerprint(request)

    token = generate_token(
        str(user.id),
        user.token_version,
        fingerprint,
    )

    response.set_cookie(
        key="cf_session",
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=60 * 60 * 24 * 30 if payload.remember_me else None,
        path="/",
    )

    user.otp_hash = None
    user.otp_expires_at = None
    user.otp_attempts = 0
    user.otp_locked_until = None
    user.otp_challenge_id = None
    user.otp_challenge_expires_at = None
    user.last_seen_at = now
    user.token_created_at = now
    user.fingerprint_hash = fingerprint
    user.last_login_ip = request.client.host if request.client else None

    audit_service.log(
        db,
        action="auth.verify",
        outcome="success",
        actor_user=user,
        target_type="user",
        target_id=str(user.id),
        ip_address=ip_address,
        user_agent=user_agent,
        metadata={"phone_number": phone_number},
    )
    db.commit()

    return VerifyAuthResponse(status="authenticated")


# ==========================================
# Current user
# ==========================================

@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user)):
    return MeResponse(
        id=str(user.id),
        full_name=user.full_name,
        phone_number=user.phone_number,
        credits=user.credits,
        status=user.status.value,
        role=user.role.value,
        is_admin=user.role == UserRole.ADMIN,
    )


# ==========================================
# Logout
# ==========================================

@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("cf_session", path="/", samesite=settings.cookie_samesite, secure=settings.cookie_secure)
    return {"status": "logged_out"}
