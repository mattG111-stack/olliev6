"""Email-verified password recovery. Never logs codes or changes account roles."""
from datetime import datetime, timedelta, timezone
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from config import settings
from db import get_db
from models import User, VerificationCode
from notify import send_email
from password_policy import validate_new_password
from security import find_user_by_email, hash_password

router = APIRouter()
CHANNEL = "password_reset"
MESSAGE = "If this email has an account, a reset code has been sent. Check your inbox and spam folder."


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ResetRequest(BaseModel):
    email: EmailStr


class ResetConfirm(ResetRequest):
    code: str = Field(pattern=r"^\d{6}$")
    password: str = Field(max_length=128)


@router.post("/forgot-password")
def request_reset(body: ResetRequest, db: Session = Depends(get_db)):
    # Do not call notify's development fallback: it prints message contents.
    if not settings.resend_api_key:
        raise HTTPException(503, "Password reset email is not configured. Contact your administrator.")
    user = find_user_by_email(db, str(body.email))
    if user is None:
        return {"detail": MESSAGE}
    # Serialize requests for this account, including when it has no codes yet.
    db.query(User).filter(User.id == user.id).with_for_update().one()
    now = datetime.now(timezone.utc)
    recent = db.query(VerificationCode).filter(
        VerificationCode.user_id == user.id, VerificationCode.channel == CHANNEL,
        VerificationCode.created_at >= now - timedelta(hours=1),
    ).order_by(VerificationCode.id.desc()).all()
    if len(recent) >= 5 or (recent and utc(recent[0].created_at) > now - timedelta(seconds=60)):
        return {"detail": MESSAGE}
    code = f"{secrets.randbelow(1_000_000):06d}"
    row = VerificationCode(user_id=user.id, channel=CHANNEL, code=code,
        expires_at=now + timedelta(minutes=15), attempts=0)
    db.add(row)
    db.flush()
    sent = send_email(user.email, "Reset your Apex password",
        f"<p>Your Apex password reset code is <strong>{code}</strong>.</p>"
        "<p>It expires in 15 minutes. If you did not request this, ignore this email.</p>")
    if not sent:
        db.rollback()
        # Same public response regardless of whether the account exists.
        return {"detail": MESSAGE}
    db.query(VerificationCode).filter(
        VerificationCode.user_id == user.id, VerificationCode.channel == CHANNEL,
        VerificationCode.id != row.id, VerificationCode.consumed_at.is_(None),
    ).update({"consumed_at": now}, synchronize_session=False)
    db.commit()
    return {"detail": MESSAGE}


@router.post("/reset-password")
def confirm_reset(body: ResetConfirm, db: Session = Depends(get_db)):
    validate_new_password(body.password)
    user = find_user_by_email(db, str(body.email))
    invalid = HTTPException(400, "Invalid or expired reset code. Request a new code.")
    if user is None:
        raise invalid
    db.query(User).filter(User.id == user.id).with_for_update().one()
    row = db.query(VerificationCode).filter(
        VerificationCode.user_id == user.id, VerificationCode.channel == CHANNEL,
        VerificationCode.consumed_at.is_(None),
    ).order_by(VerificationCode.id.desc()).with_for_update().first()
    now = datetime.now(timezone.utc)
    if row is None or utc(row.expires_at) <= now or row.attempts >= 6:
        raise invalid
    row.attempts += 1
    if not secrets.compare_digest(row.code, body.code):
        db.commit()
        raise invalid
    user.password_hash = hash_password(body.password)
    row.consumed_at = now
    db.commit()
    return {"detail": "Password changed. Sign in with your new password."}
