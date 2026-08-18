"""JWT auth + password hashing + role guards."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from passlib.exc import UnknownHashError
from sqlalchemy import func
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User, UserRole, UserStatus

log = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/sign-in", auto_error=True)

# The bcrypt library, used directly as a version-robust fallback. passlib 1.7.4's
# bcrypt backend can break across bcrypt releases (it reads bcrypt.__about__,
# removed in bcrypt 4.1) and then throws on EVERY hash/verify — so a redeploy that
# pulled a newer bcrypt makes all logins fail with "not a valid hash" even though
# the stored $2b$ hashes are perfectly valid. Calling bcrypt directly sidesteps
# that entirely. bcrypt caps the password at 72 bytes; we mirror that.
try:
    import bcrypt as _bcrypt
except Exception:  # pragma: no cover - bcrypt is a hard dependency
    _bcrypt = None


class PasswordHashingUnavailable(RuntimeError):
    """bcrypt cannot hash on this server, with the reason attached.

    Creating a user and setting a password both hash; signing in only verifies,
    and verification swallows every error and answers "wrong password". So a
    broken bcrypt install shows up as 500 on those two admin actions and 401 on
    every login — which is exactly the set of symptoms reported, and none of them
    say the word bcrypt. This carries the reason out to where it can be read.
    """


def hash_password(password: str) -> str:
    errors = []
    try:
        return pwd_context.hash(password)
    except Exception as exc:                      # passlib/bcrypt version mismatch
        errors.append(f"passlib: {type(exc).__name__}: {exc}")
    if _bcrypt is not None:
        try:
            return _bcrypt.hashpw(password.encode("utf-8")[:72],
                                  _bcrypt.gensalt()).decode("utf-8")
        except Exception as exc:
            errors.append(f"bcrypt: {type(exc).__name__}: {exc}")
    else:
        errors.append("bcrypt: the library failed to import")
    log.error("password hashing unavailable: %s", " | ".join(errors))
    raise PasswordHashingUnavailable(
        "Cannot hash passwords on this server — " + " | ".join(errors)
    )


def hashing_selftest() -> dict:
    """Can this server actually hash and verify a password? Reported by
    /api/admin/diagnostics, because every symptom of it failing points somewhere
    else."""
    import importlib

    out: dict = {}
    for mod in ("bcrypt", "passlib"):
        try:
            m = importlib.import_module(mod)
            out[mod] = getattr(m, "__version__", "unknown")
        except Exception as exc:
            out[mod] = f"NOT IMPORTABLE: {type(exc).__name__}: {exc}"
    try:
        h = hash_password("selftest-password")
        out["hash"] = f"ok ({len(h)} chars, starts {h[:4]!r})"
        out["verify"] = "ok" if verify_password("selftest-password", h) else "FAILED to verify its own hash"
    except Exception as exc:
        out["hash"] = f"{type(exc).__name__}: {exc}"
        out["verify"] = "not attempted"
    return out


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password, tolerating a malformed stored hash OR a passlib/bcrypt
    version mismatch.

    passlib raises UnknownHashError when password_hash holds something that
    isn't a recognisable hash — an empty string, or plain text written by a
    direct database edit — and can raise ValueError when its bcrypt backend is
    incompatible with the installed bcrypt library. Uncaught, that surfaces as a
    500 (or a mass login failure) rather than the credentials being wrong.

    First try passlib; if it throws, fall back to verifying a real $2 bcrypt hash
    directly with the bcrypt library, which is robust across bcrypt versions.
    Only a hash that BOTH paths reject is treated as unverifiable (failed login).
    """
    if not hashed or not isinstance(hashed, str):
        return False
    try:
        return pwd_context.verify(plain, hashed)
    except (UnknownHashError, ValueError):
        pass
    except Exception:
        pass
    if _bcrypt is not None and hashed.startswith("$2"):
        try:
            return _bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
        except (ValueError, TypeError):
            pass
    log.warning("password_hash is not a valid hash; treating as failed login")
    return False


def find_user_by_email(db: Session, email: str | None) -> User | None:
    """Look an account up by email, case-insensitively and ignoring stray space.

    Every route that touches an account by address must go through this. An exact
    `User.email == email.lower()` comparison can never match a row stored with
    capitals or a trailing space, which breaks in two directions: sign-in refuses
    the right password with a 401 that looks exactly like a wrong one, and the
    duplicate checks on sign-up miss the existing row and let a second account be
    created for the same person — after which the sign-in lookup has two rows to
    choose from and takes whichever comes first.
    """
    e = (email or "").strip().lower()
    if not e:
        return None
    # lower() is the same everywhere; trim() is not — the SQL standard spells it
    # trim(BOTH FROM x) and dialects differ on whether a bare trim(x) is even a
    # function. Relying on it put the whole of sign-in and every admin user
    # action on a construct that may not exist on the database actually running.
    row = db.query(User).filter(func.lower(User.email) == e).first()
    if row is not None:
        return row
    # Whitespace in a stored address is rare and the account list is small, so a
    # scan is the cheap, portable way to still find it. _normalise_emails cleans
    # these up at boot, so this is a safety net, not the normal path.
    for u in db.query(User).all():
        if (u.email or "").strip().lower() == e:
            return u
    return None


def create_access_token(user_id: int) -> tuple[str, datetime]:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expiry_minutes)
    payload = {"sub": str(user_id), "exp": expires}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires


def _credentials_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_user(token: str = Depends(oauth2), db: Session = Depends(get_db)) -> User:
    """Any authenticated, non-rejected/deactivated user. PENDING users are allowed
    through so they can complete self-serve onboarding (verify + add card); product
    access is gated separately by require_active."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise _credentials_error()
    user = db.get(User, user_id)
    if user is None:
        raise _credentials_error()
    if user.status in (UserStatus.REJECTED.value, UserStatus.DEACTIVATED.value):
        raise HTTPException(status_code=403, detail=f"Account is {user.status}")
    return user


# Stripe states that mean the customer is in their trial or actively paying.
ACTIVE_SUBSCRIPTION_STATES = {"trialing", "active"}


def has_product_access(user: User) -> bool:
    """True if the user may see the product. Admins and admin-approved users get
    in outright (they bypass billing); everyone else needs a live subscription —
    i.e. they've added a card and are trialing or paying."""
    if user.role == UserRole.ADMIN.value:
        return True
    # A promoter is not a customer. Their account is created approved so they can
    # sign in and reach their referral dashboard, and "approved" is the flag that
    # otherwise waves someone past billing — so without this line every promoter
    # would quietly hold a free copy of the paid product. Checked BEFORE status
    # for exactly that reason.
    if user.role == UserRole.PROMOTER.value:
        return False
    if user.status == UserStatus.APPROVED.value:
        return True
    return (user.subscription_status or "") in ACTIVE_SUBSCRIPTION_STATES


def require_active(user: User = Depends(current_user)) -> User:
    """Gate for the actual product. Returns 402 (Payment Required) when the user
    is authenticated but hasn't finished onboarding, so the frontend can route
    them to the paywall rather than treating it as a hard forbidden."""
    if not has_product_access(user):
        raise HTTPException(status_code=402, detail="onboarding_incomplete")
    return user


def require_promoter(user: User = Depends(current_user)) -> User:
    """Gate for the promoter dashboard.

    Deliberately NOT behind has_product_access. A promoter is not a customer:
    they have no subscription and see no listings, so the paywall would lock
    them out of the one page they exist to look at. Admins are let through so
    they can see what a promoter sees.
    """
    if user.role not in (UserRole.PROMOTER.value, UserRole.ADMIN.value):
        raise HTTPException(status_code=403, detail="Promoter only")
    if user.status == UserStatus.DEACTIVATED.value:
        raise HTTPException(status_code=403, detail="Account is deactivated")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(status_code=403, detail="Admin only")
    if user.status != UserStatus.APPROVED.value:
        raise HTTPException(status_code=403, detail=f"Account is {user.status}")
    return user


def _repair_plaintext_hashes(db: Session) -> None:
    """Re-hash any password_hash that isn't a real bcrypt hash.

    A direct database edit can leave plain text in password_hash. That user can
    then never log in: passlib can't identify the value, and every attempt is a
    failed login. Rather than locking the account out permanently, we treat the
    stored text as the intended password and hash it properly.

    This only ever moves a row from plain text (bad) to bcrypt (good) — it never
    weakens an existing hash. A real bcrypt hash starts with $2 and is 60 chars,
    so anything else is broken by definition.
    """
    try:
        rows = db.query(User).all()
    except Exception:
        return  # table may not exist yet on a first boot
    fixed = 0
    for u in rows:
        h = u.password_hash or ""
        if isinstance(h, str) and h.startswith("$2") and len(h) == 60:
            continue  # already a valid bcrypt hash
        if not h:
            continue  # nothing to recover; leave it for a password reset
        u.password_hash = hash_password(h)
        fixed += 1
    if fixed:
        db.commit()
        log.warning("repaired %d password hash(es) that were not bcrypt", fixed)


def _normalise_emails(db: Session) -> None:
    """Lowercase and trim any stored email that isn't already.

    Sign-in looks accounts up by a lowercased, trimmed address. A row holding
    "Matt.Grant@Outlook.co.nz" — from a hand-written insert, an import, or a
    version of the sign-up route that predates the normalisation — cannot be
    matched by ANY input: not the address as typed, because that gets lowered
    before the comparison, and not the lowered form, because the row still has
    capitals. The account answers 401 with the correct password and looks exactly
    like a typo.

    The lookup is now case-insensitive regardless, so this is about the data
    rather than the query: leaving mixed-case rows in place means every future
    lookup pays for a function call on the column and cannot use the index.

    A row is skipped if normalising it would collide with an existing account —
    two rows differing only by case are a merge decision, not a repair.
    """
    try:
        rows = db.query(User).all()
    except Exception:
        return
    taken = {(u.email or "").strip().lower() for u in rows}
    fixed = 0
    for u in rows:
        raw = u.email or ""
        norm = raw.strip().lower()
        if raw == norm or not norm:
            continue
        clash = any((o.email or "") == norm for o in rows if o.id != u.id)
        if clash:
            log.warning("email %r not normalised: %r already exists", raw, norm)
            continue
        u.email = norm
        fixed += 1
    if fixed:
        db.commit()
        log.warning("normalised %d email address(es) that could never sign in", fixed)


def ensure_seed_admin(db: Session) -> None:
    """Wrapper: a boot-time repair must never take the server down with it.

    This runs inside the app's lifespan. Anything it raises stops startup, and a
    server that will not start cannot tell anyone why — you get a crash loop and
    a blank page. Every failure in here is a repair that did not happen, which is
    strictly better than no application.
    """
    try:
        _ensure_seed_admin(db)
    except Exception:
        log.exception("seed admin setup failed; continuing without it")
        try:
            db.rollback()
        except Exception:
            pass


def _ensure_seed_admin(db: Session) -> None:
    """Guarantee the seed admin can log in with the CURRENT env credentials.

    Previously this only created the admin on a truly empty DB and never touched
    an existing one — so once any admin existed, changing SEED_ADMIN_PASSWORD in
    the environment did nothing and an operator who didn't know the original
    password was locked out. Now, on every boot, the account matching
    SEED_ADMIN_EMAIL is re-synced to the env password (and forced to an active
    admin). Set SEED_ADMIN_EMAIL + SEED_ADMIN_PASSWORD, redeploy, and those exact
    credentials log you in — no matter what state the DB was left in.
    """
    _repair_plaintext_hashes(db)
    _normalise_emails(db)
    # Sign-in looks the account up by email.lower() (routers/auth.sign_in), so the
    # stored email MUST be lowercase or the credentials never match and every login
    # 401s — even with the right password. Normalise here so a SEED_ADMIN_EMAIL that
    # was typed with any capital letter still works.
    seed_email = (settings.seed_admin_email or "").strip().lower()
    admin = find_user_by_email(db, seed_email)
    if admin is None:
        admin = User(
            email=seed_email,
            password_hash=hash_password(settings.seed_admin_password),
            full_name="Seed Admin",
            role=UserRole.ADMIN.value,
            status=UserStatus.APPROVED.value,
        )
        db.add(admin)
        db.commit()
        log.warning("seed admin CREATED: sign in as %s with SEED_ADMIN_PASSWORD", seed_email)
        return
    # Account exists — re-sync it to the env password and ensure it's an active
    # admin, so the credentials set in the environment always work.
    admin.password_hash = hash_password(settings.seed_admin_password)
    admin.role = UserRole.ADMIN.value
    admin.status = UserStatus.APPROVED.value
    db.commit()
    # Say so in the boot log. A 401 gives the same answer whether the email is
    # wrong, the password is wrong, or the account was never created, and with no
    # way in there is no way to tell them apart. This names the one account that
    # is guaranteed to work after this boot. The email only — never the password.
    log.warning("seed admin ready: sign in as %s with SEED_ADMIN_PASSWORD", seed_email)
