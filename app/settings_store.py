"""Account-wide settings an admin sets, and the daily cap on the shared LLM key.

Kept out of the routers so the assistant, the admin endpoints and the quota
readout all agree on what "configured" and "used today" mean. Every read is
best-effort: on a database that has not built app_settings yet, the shared key
simply reads as absent and the per-user key path still works, rather than the
assistant 500ing on a boot ordering problem.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from .assistant import keys
from .models import (
    DEFAULT_DAILY_LIMIT,
    LLM_API_KEY,
    LLM_DAILY_LIMIT,
    LLM_PROVIDER,
    AppSetting,
    AssistantLog,
    User,
)

log = logging.getLogger(__name__)


class SettingsUnavailable(RuntimeError):
    """The settings store could not be read or written, with the reason attached.

    Carried to the caller so the admin panel can show what is actually wrong. A
    bare 500 in a browser console says only that something broke on a server the
    person reading it cannot see the logs of.
    """

# The working day is the user's, not UTC's. On UTC the allowance would reset at
# midday in Auckland, so a morning's questions and an afternoon's would land in
# different days and the cap would mean something different depending on when
# you started.
NZ_TZ = timezone(timedelta(hours=12))


def ensure_table() -> None:
    """Create app_settings if it is missing.

    db_bootstrap runs create_all on every boot, which should already have built
    it — but that call is wrapped in a catch-all so a failure there prints a line
    and startup continues. When it does fail, the first symptom is every admin
    assistant endpoint answering 500, with the actual cause only in a boot log
    nobody is reading by then. Creating the one table on demand costs a cheap
    no-op per call and removes that whole failure mode.
    """
    try:
        AppSetting.__table__.create(bind=_engine(), checkfirst=True)
    except Exception:
        log.exception("could not create app_settings")


def _engine():
    from .db import engine
    return engine


def get(db: Session, key: str) -> str | None:
    try:
        row = db.get(AppSetting, key)
    except Exception:
        # A failed statement leaves a Postgres transaction unusable, so every
        # later query in the same request fails too with a misleading error.
        db.rollback()
        ensure_table()
        try:
            row = db.get(AppSetting, key)
        except Exception:
            db.rollback()
            return None
    return row.value if row else None


def put(db: Session, key: str, value: str | None, *, by: int | None = None) -> None:
    """Write one setting. Raises SettingsUnavailable if the store cannot be used,
    so the caller can say what happened instead of returning a bare 500."""
    for attempt in (1, 2):
        try:
            row = db.get(AppSetting, key)
            if row is None:
                row = AppSetting(key=key)
                db.add(row)
            row.value = value
            row.updated_at = datetime.now(timezone.utc)
            row.updated_by = by
            db.commit()
            return
        except Exception as exc:
            db.rollback()
            if attempt == 1:
                ensure_table()
                continue
            log.exception("could not write setting %s", key)
            raise SettingsUnavailable(
                f"Could not save settings: {type(exc).__name__}: {exc}"
            ) from exc


def shared_key(db: Session) -> tuple[str | None, str | None]:
    """(provider, plaintext api key) for the account-wide key, or (None, None)."""
    provider = (get(db, LLM_PROVIDER) or "").strip() or None
    stored = get(db, LLM_API_KEY)
    if not provider or not stored:
        return None, None
    plain = keys.decrypt(stored)
    if not plain:
        # Encrypted with a different ASSISTANT_KEY_SECRET than the one running —
        # the ciphertext is intact but unreadable, so it is not a usable key.
        log.warning("shared assistant key could not be decrypted; re-enter it in the admin panel")
        return None, None
    return provider, plain


def daily_limit(db: Session) -> int:
    raw = get(db, LLM_DAILY_LIMIT)
    try:
        n = int(str(raw))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LIMIT
    return max(0, n)


def _day_start() -> datetime:
    """Midnight tonight-just-gone in New Zealand, as an aware UTC-comparable time."""
    now_nz = datetime.now(NZ_TZ)
    return datetime.combine(now_nz.date(), time.min, tzinfo=NZ_TZ)


def used_today(db: Session, user_id: int | None) -> int:
    """Answers this user has been given today.

    Counts successful answers only. A question that errored gave them nothing,
    and burning someone's daily allowance on our own provider outage is the kind
    of small unfairness nobody can see the cause of.
    """
    if user_id is None:
        return 0
    try:
        return (db.query(AssistantLog)
                .filter(AssistantLog.user_id == user_id,
                        AssistantLog.ok.is_(True),
                        AssistantLog.created_at >= _day_start())
                .count())
    except Exception:
        db.rollback()   # keep the session usable for the rest of the request
        return 0


def user_has_own_key(user: User) -> bool:
    return bool((user.llm_provider or "").strip() and keys.decrypt(user.llm_api_key_encrypted))


def quota_for(db: Session, user: User) -> dict:
    """What the UI needs to show, and what the ask endpoint enforces.

    A user with their own key is unlimited — they are paying for every call, and
    capping someone else's spend would be strange. Everyone else shares the
    account key and gets the daily allowance.
    """
    if user_has_own_key(user):
        return {"shared": False, "limit": None, "used": 0, "remaining": None,
                "configured": True}
    provider, key = shared_key(db)
    limit = daily_limit(db)
    used = used_today(db, user.id)
    return {"shared": True, "limit": limit, "used": used,
            "remaining": max(0, limit - used), "configured": bool(provider and key)}
