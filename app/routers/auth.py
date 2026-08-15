"""Auth + self-serve onboarding + admin user-management endpoints."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import User, UserRole, UserStatus
from ..notify import send_email, send_sms, verification_email_html
from ..security import (
    create_access_token,
    current_user,
    find_user_by_email,
    has_product_access,
    hash_password,
    require_admin,
    verify_password,
)
from ..verification import check_code, issue_code

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _next_step(u: User) -> str:
    """Where this user is in the self-serve onboarding funnel."""
    if has_product_access(u):
        return "done"
    if u.email_verified_at is None:
        return "verify_email"
    if u.phone_verified_at is None:
        return "verify_phone"
    return "add_card"


def _send_email_code(db: Session, u: User) -> None:
    code = issue_code(db, u.id, "email")
    send_email(u.email, "Your Apex Property verification code",
               verification_email_html(code, settings.verify_code_ttl_minutes),
               text=f"Your Apex Property verification code is {code} (valid {settings.verify_code_ttl_minutes} min).")


def _send_phone_code(db: Session, u: User) -> None:
    code = issue_code(db, u.id, "phone")
    send_sms(u.phone or "", f"Your Apex Property code is {code}")


class SignUpIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None
    company: str | None = None
    phone: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str | None
    company: str | None
    phone: str | None
    role: str
    status: str

    class Config:
        from_attributes = True


class MeOut(UserOut):
    """UserOut plus onboarding state, so the app can route to the right step."""
    email_verified: bool
    phone_verified: bool
    subscription_status: str | None
    trial_ends_at: datetime | None
    has_access: bool
    next_step: str
    llm_key_managed: bool = False


def _me_out(u: User) -> MeOut:
    return MeOut(
        id=u.id, email=u.email, full_name=u.full_name, company=u.company, phone=u.phone,
        role=u.role, status=u.status,
        email_verified=u.email_verified_at is not None,
        phone_verified=u.phone_verified_at is not None,
        subscription_status=u.subscription_status,
        trial_ends_at=u.trial_ends_at,
        has_access=has_product_access(u),
        next_step=_next_step(u),
        llm_key_managed=bool(u.llm_key_managed),
    )


class SignUpOut(BaseModel):
    """Sign-up authenticates immediately so the client can start onboarding."""
    token: TokenOut
    user: MeOut


@router.post("/sign-up", response_model=SignUpOut, status_code=201)
def sign_up(body: SignUpIn, db: Session = Depends(get_db)) -> SignUpOut:
    if find_user_by_email(db, str(body.email)):
        raise HTTPException(status_code=409, detail="Email already registered")
    u = User(
        email=str(body.email).lower(),
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        company=body.company,
        phone=body.phone,
        role=UserRole.USER.value,
        status=UserStatus.PENDING.value,
        signup_source="self",
    )
    db.add(u); db.commit(); db.refresh(u)
    # Confirm in the log that the row landed, and with what. "I signed up and
    # then could not log in" is two possible stories — the account was never
    # created, or it was created and the password does not verify — and this
    # line is what tells them apart.
    log.warning("account created (self sign-up): %s id=%s status=%s", u.email, u.id, u.status)
    _send_email_code(db, u)                       # kick off email verification
    token, expires = create_access_token(u.id)
    return SignUpOut(token=TokenOut(access_token=token, expires_at=expires), user=_me_out(u))


@router.post("/sign-in", response_model=TokenOut)
def sign_in(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)) -> TokenOut:
    # Case-insensitive, space-tolerant — see security.find_user_by_email. Rows
    # created through the API are normalised on the way in, but rows that predate
    # that, or were written by hand, are not, and an exact match refuses them with
    # the right password.
    u = find_user_by_email(db, form.username)
    if not u or not verify_password(form.password, u.password_hash):
        # Say WHY in the server log. The response stays a bare 401 on purpose —
        # telling the browser "no such account" would let anyone test which
        # addresses are registered. But the operator needs to tell apart an
        # account that does not exist, a wrong password, and a stored hash that
        # cannot be verified at all, and until now a 401 answered all three
        # identically. Being locked out with no way to tell which one you are
        # looking at is how a login bug survives several rounds of fixes.
        if not u:
            log.warning("sign-in refused for %r: no account with that email "
                        "(%d accounts exist)", form.username, db.query(User).count())
        else:
            h = u.password_hash or ""
            looks_hashed = h.startswith("$2") and len(h) == 60
            log.warning("sign-in refused for %r: password did not match "
                        "(account id=%s, status=%s, stored hash %s)",
                        form.username, u.id, u.status,
                        "looks fine" if looks_hashed
                        else f"is NOT a bcrypt hash: {len(h)} chars, starts {h[:4]!r}")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # Rejected/deactivated can't sign in. PENDING self-serve users CAN — they need
    # to finish onboarding (verify + add card); the product itself is gated by
    # require_active, so signing in just lets them reach the onboarding steps.
    if u.status in (UserStatus.REJECTED.value, UserStatus.DEACTIVATED.value):
        raise HTTPException(status_code=403, detail=f"Account {u.status}")
    token, expires = create_access_token(u.id)
    u.last_login_at = datetime.now(timezone.utc)
    u.login_count = (u.login_count or 0) + 1
    db.commit()
    return TokenOut(access_token=token, expires_at=expires)


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(current_user)) -> MeOut:
    return _me_out(user)


# ---- Self-serve onboarding: verify email + phone ----
class CodeIn(BaseModel):
    code: str


@router.post("/verify/email/send")
def send_email_code(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if user.email_verified_at is not None:
        return {"sent": False, "already_verified": True}
    _send_email_code(db, user)
    return {"sent": True}


@router.post("/verify/email", response_model=MeOut)
def verify_email(body: CodeIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> MeOut:
    if user.email_verified_at is not None:
        return _me_out(user)
    ok, reason = check_code(db, user.id, "email", body.code)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    user.email_verified_at = datetime.now(timezone.utc)
    db.commit(); db.refresh(user)
    return _me_out(user)


@router.post("/verify/phone/send")
def send_phone_code(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if user.phone_verified_at is not None:
        return {"sent": False, "already_verified": True}
    if not user.phone:
        raise HTTPException(status_code=400, detail="no_phone_on_file")
    _send_phone_code(db, user)
    return {"sent": True}


@router.post("/verify/phone", response_model=MeOut)
def verify_phone(body: CodeIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> MeOut:
    if user.phone_verified_at is not None:
        return _me_out(user)
    ok, reason = check_code(db, user.id, "phone", body.code)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    user.phone_verified_at = datetime.now(timezone.utc)
    db.commit(); db.refresh(user)
    return _me_out(user)


# ---- Admin user management ----
admin_router = APIRouter(prefix="/api/admin/users", tags=["admin"])


@admin_router.get("", response_model=list[UserOut])
def list_users(
    status: str | None = None,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[User]:
    q = db.query(User).order_by(User.created_at.desc())
    if status:
        q = q.filter(User.status == status)
    return q.all()


class AdminCreateUserIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None
    company: str | None = None
    phone: str | None = None
    role: str = "user"          # 'user' | 'admin'
    status: str = "approved"    # admin-created users are active by default


@admin_router.post("", response_model=UserOut, status_code=201)
def admin_create_user(body: AdminCreateUserIn, _: User = Depends(require_admin),
                      db: Session = Depends(get_db)) -> User:
    """Admin creates a user directly (no sign-up/approval flow)."""
    email = body.email.lower().strip()
    if find_user_by_email(db, email):
        raise HTTPException(status_code=409, detail="A user with that email already exists")
    if body.role not in (UserRole.USER.value, UserRole.ADMIN.value):
        raise HTTPException(status_code=400, detail="Invalid role")
    if body.status not in {s.value for s in UserStatus}:
        raise HTTPException(status_code=400, detail="Invalid status")
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    u = User(
        email=email, password_hash=hash_password(body.password),
        full_name=body.full_name, company=body.company, phone=body.phone,
        role=body.role, status=body.status, signup_source="admin",
    )
    db.add(u); db.commit(); db.refresh(u)
    log.warning("account created (by admin): %s id=%s status=%s role=%s",
                u.email, u.id, u.status, u.role)
    return u


class AdminPasswordIn(BaseModel):
    password: str


@admin_router.post("/{user_id}/password", response_model=UserOut)
def admin_set_password(
    user_id: int,
    body: AdminPasswordIn,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> User:
    """Set a user's password directly.

    Sign-in answers a wrong password with a bare 401, which is right — telling a
    stranger whether an account exists is worse than being unhelpful. But it also
    means an operator who mistypes a password when creating an account has no way
    back in and no way to tell what went wrong. This is that way back.
    """
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    u.password_hash = hash_password(body.password)
    db.commit(); db.refresh(u)
    return u


class ApprovalIn(BaseModel):
    """Everything an admin can change about an account except the password.

    Every field is optional so the original status-only call still works; a field
    left out is left alone, rather than being overwritten with a null.
    """
    status: str | None = None      # 'approved' | 'rejected' | 'deactivated' | 'pending'
    email: EmailStr | None = None
    full_name: str | None = None
    company: str | None = None
    phone: str | None = None
    role: str | None = None        # 'user' | 'admin'


def _admin_count(db: Session, exclude_id: int | None = None) -> int:
    """Admins who can still sign in. PENDING/REJECTED/DEACTIVATED admins cannot,
    so they do not count towards not locking everyone out."""
    q = db.query(User).filter(User.role == UserRole.ADMIN.value,
                              User.status == UserStatus.APPROVED.value)
    if exclude_id is not None:
        q = q.filter(User.id != exclude_id)
    return q.count()


@admin_router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: ApprovalIn,
    me: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> User:
    """Edit an account: status, email, name, company, phone, role."""
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    if body.status is not None:
        if body.status not in {s.value for s in UserStatus}:
            raise HTTPException(status_code=400, detail="Invalid status")
        # Do not let the last working admin be suspended. Whoever does it is
        # locked out the moment their token expires, and there is then nobody
        # left who can undo it.
        if (u.role == UserRole.ADMIN.value and body.status != UserStatus.APPROVED.value
                and _admin_count(db, exclude_id=u.id) == 0):
            raise HTTPException(status_code=409,
                                detail="This is the last active admin — promote another admin first")
        u.status = body.status

    if body.role is not None:
        if body.role not in (UserRole.USER.value, UserRole.ADMIN.value):
            raise HTTPException(status_code=400, detail="Invalid role")
        if (u.role == UserRole.ADMIN.value and body.role != UserRole.ADMIN.value
                and _admin_count(db, exclude_id=u.id) == 0):
            raise HTTPException(status_code=409,
                                detail="This is the last active admin — promote another admin first")
        u.role = body.role

    if body.email is not None:
        email = str(body.email).strip().lower()
        clash = find_user_by_email(db, email)
        if clash and clash.id != u.id:
            raise HTTPException(status_code=409, detail="A user with that email already exists")
        u.email = email

    for field in ("full_name", "company", "phone"):
        v = getattr(body, field)
        if v is not None:
            setattr(u, field, v.strip() or None)

    db.commit(); db.refresh(u)
    log.warning("account edited by admin %s: %s id=%s status=%s role=%s",
                me.email, u.email, u.id, u.status, u.role)
    return u


@admin_router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    me: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    """Delete an account for good.

    Two things this deliberately does NOT do. It will not delete the account you
    are signed in as — you would be removing the session you are holding, and the
    next request would 401 with no way back. And it will not delete the last
    active admin, which locks everyone out of admin permanently.

    Rows the user owned outright — their wishlist, their verification codes — go
    with them. Rows they merely touched do not: an import batch is the data
    itself, and deleting a colleague should not delete the week's listings, so
    those references are detached and the records stay.
    """
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if u.id == me.id:
        raise HTTPException(status_code=409, detail="You cannot delete the account you are signed in as")
    if u.role == UserRole.ADMIN.value and _admin_count(db, exclude_id=u.id) == 0:
        raise HTTPException(status_code=409,
                            detail="This is the last active admin — promote another admin first")

    from ..models import (AgentContact, AssistantLog, ImportBatch, IngestJob,
                          VerificationCode, WishList)

    for model in (WishList, VerificationCode):
        db.query(model).filter(model.user_id == u.id).delete(synchronize_session=False)
    for model, col in ((AssistantLog, "user_id"), (AgentContact, "user_id"),
                       (ImportBatch, "uploaded_by_id"), (IngestJob, "uploaded_by_id")):
        db.query(model).filter(getattr(model, col) == u.id).update(
            {col: None}, synchronize_session=False)
    try:
        from ..models import BuildingOverride
        db.query(BuildingOverride).filter(BuildingOverride.updated_by == u.id).update(
            {"updated_by": None}, synchronize_session=False)
    except Exception:
        pass   # table may not exist yet on an older database

    email = u.email
    db.delete(u)
    db.commit()
    log.warning("account DELETED by admin %s: %s id=%s", me.email, email, user_id)
    return Response(status_code=204)
