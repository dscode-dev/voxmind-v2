import hashlib
import random
import secrets
from datetime import datetime, timedelta


OTP_LENGTH = 6
OTP_EXP_MINUTES = 5
OTP_CHALLENGE_EXP_MINUTES = 10


def generate_otp() -> str:
    return "".join(str(random.randint(0, 9)) for _ in range(OTP_LENGTH))


def hash_otp(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def verify_otp(code: str, otp_hash: str | None) -> bool:
    if not otp_hash:
        return False
    return hashlib.sha256(code.encode()).hexdigest() == otp_hash


def otp_expiration() -> datetime:
    return datetime.utcnow() + timedelta(minutes=OTP_EXP_MINUTES)


def generate_challenge_id() -> str:
    return "otp_" + secrets.token_urlsafe(24)


def challenge_expiration() -> datetime:
    return datetime.utcnow() + timedelta(minutes=OTP_CHALLENGE_EXP_MINUTES)