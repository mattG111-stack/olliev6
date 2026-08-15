"""Ask-anything endpoint, plus the Settings screen's key management.

Answers are grounded in tool calls against real data. The user brings their own
Claude or OpenAI key; it is stored encrypted and never returned to the client.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..assistant import keys, providers
from ..assistant.agent import AssistantUnavailable, Turn, ask
from ..db import get_db
from ..models import (DEFAULT_DAILY_LIMIT, LLM_API_KEY, LLM_DAILY_LIMIT,
                      LLM_PROVIDER, AppSetting, AssistantLog, User)
from .. import settings_store
from ..security import require_active, require_admin

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


# --- settings --------------------------------------------------------------

class KeyStatus(BaseModel):
    configured: bool
    provider: str | None = None
    # Enough to recognise which key is saved, never the key itself.
    key_last_four: str | None = None
    updated_at: datetime | None = None
    detail: str


class SaveKeyIn(BaseModel):
    provider: str = Field(pattern="^(anthropic|openai)$")
    api_key: str = Field(min_length=10, max_length=400)


@router.get("/settings", response_model=KeyStatus)
def get_settings(me: User = Depends(require_active)) -> KeyStatus:
    plain = keys.decrypt(me.llm_api_key_encrypted)
    if me.llm_api_key_encrypted and not plain:
        # Stored value exists but won't decrypt — jwt_secret was rotated.
        return KeyStatus(configured=False, provider=me.llm_provider,
                         detail="Your saved key could not be read. Please re-enter it.")
    if not plain:
        return KeyStatus(configured=False,
                         detail="Add a Claude or OpenAI API key to use the assistant.")
    return KeyStatus(
        configured=True, provider=me.llm_provider,
        key_last_four=keys.last_four(me.llm_api_key_encrypted),
        updated_at=me.llm_key_updated_at,
        detail=f"Connected to {me.llm_provider}.",
    )


@router.put("/settings", response_model=KeyStatus)
def save_key(body: SaveKeyIn, me: User = Depends(require_active),
             db: Session = Depends(get_db)) -> KeyStatus:
    """Validate the key with a live call, then store it encrypted.

    Testing before saving means a typo is caught here rather than surfacing as
    a confusing failure on the user's first question.
    """
    if me.llm_key_managed:
        raise HTTPException(status_code=403, detail="The AI assistant is enabled by your administrator and can't be changed here.")
    api_key = body.api_key.strip()
    if msg := keys.looks_valid(body.provider, api_key):
        raise HTTPException(status_code=400, detail=msg)

    try:
        providers.run(
            provider=body.provider, api_key=api_key,
            system="Reply with the single word: ok",
            messages=[{"role": "user", "content": "ok"}],
            specs=[], dispatch=lambda *_: "",
        )
    except providers.ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    user = db.get(User, me.id)
    user.llm_provider = body.provider
    user.llm_api_key_encrypted = keys.encrypt(api_key)
    user.llm_key_updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)

    return KeyStatus(
        configured=True, provider=user.llm_provider,
        key_last_four=keys.last_four(user.llm_api_key_encrypted),
        updated_at=user.llm_key_updated_at,
        detail=f"Connected to {user.llm_provider}. Your key is saved.",
    )


@router.delete("/settings", response_model=KeyStatus)
def delete_key(me: User = Depends(require_active),
               db: Session = Depends(get_db)) -> KeyStatus:
    if me.llm_key_managed:
        raise HTTPException(status_code=403, detail="The AI assistant is enabled by your administrator and can't be removed here.")
    user = db.get(User, me.id)
    user.llm_provider = None
    user.llm_api_key_encrypted = None
    user.llm_key_updated_at = None
    db.commit()
    return KeyStatus(configured=False, detail="Key removed.")


# --- asking ----------------------------------------------------------------

class TurnIn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    # Prior turns, oldest first. The API is stateless — the client owns history.
    history: list[TurnIn] = Field(default_factory=list, max_length=20)


class AskOut(BaseModel):
    answer: str
    tools_used: list[str]
    iterations: int
    # The SQL the model ran, so any answer can be audited.
    queries: list[str]


@router.post("", response_model=AskOut)
def ask_question(body: AskIn, me: User = Depends(require_active),
                 db: Session = Depends(get_db)) -> AskOut:
    # Memory: if the client sent no prior turns (a fresh session), seed the context
    # with this user's own recent Q&A so Ollie remembers what they've been exploring
    # across visits. Best-effort — never let it block a question.
    turns = [Turn(role=t.role, content=t.content) for t in body.history]
    if not turns:
        try:
            turns = _recent_memory(db, me.id)
        except Exception:
            turns = []

    # The daily cap, enforced only on the account-wide key. Someone using their
    # own key is spending their own money and is not counted.
    quota = settings_store.quota_for(db, me)
    if quota["shared"] and quota["configured"] and quota["remaining"] <= 0:
        raise HTTPException(
            status_code=429,
            detail=f"You have used your {quota['limit']} questions for today. "
                   f"The allowance resets at midnight. Add your own API key in "
                   f"Settings for unlimited questions.",
        )

    try:
        result = ask(me, body.question, turns, shared=settings_store.shared_key(db))
    except AssistantUnavailable as exc:
        # 428 Precondition Required — the UI reads this as "send them to Settings".
        raise HTTPException(status_code=428, detail=str(exc)) from exc
    except providers.ProviderError as exc:
        _log_question(db, me.id, body.question, answer=None, ok=False, tools=None)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Store every question + answer — the corpus Ollie learns from.
    _log_question(db, me.id, body.question, answer=result.text, ok=True,
                  tools=result.tools_used)
    return AskOut(answer=result.text, tools_used=result.tools_used,
                  iterations=result.iterations, queries=result.queries)


class QuotaOut(BaseModel):
    """What the Ask page shows above the box, so nobody meets the cap by surprise."""
    shared: bool                 # using the account key rather than their own
    configured: bool             # a usable key exists at all
    limit: int | None            # None = unlimited (their own key)
    used: int
    remaining: int | None


@router.get("/quota", response_model=QuotaOut)
def quota(me: User = Depends(require_active), db: Session = Depends(get_db)) -> QuotaOut:
    return QuotaOut(**settings_store.quota_for(db, me))


def _log_question(db: Session, user_id: int | None, question: str,
                  *, answer: str | None, ok: bool, tools: list[str] | None) -> None:
    """Persist one Q&A. Best-effort: a logging failure must never break the answer."""
    try:
        db.add(AssistantLog(user_id=user_id, question=question[:8000],
                            answer=(answer or "")[:16000] or None, ok=ok,
                            tools_used=json.dumps(tools) if tools else None))
        db.commit()
    except Exception:
        db.rollback()


def _recent_memory(db: Session, user_id: int | None, limit: int = 3) -> list[Turn]:
    """This user's last few good Q&A pairs, oldest first — fed back as context so
    the assistant carries what it has already learned about their interests."""
    if not user_id:
        return []
    rows = (db.query(AssistantLog)
            .filter(AssistantLog.user_id == user_id, AssistantLog.ok.is_(True),
                    AssistantLog.answer.isnot(None))
            .order_by(AssistantLog.id.desc()).limit(limit).all())
    turns: list[Turn] = []
    for r in reversed(rows):   # oldest first
        turns.append(Turn(role="user", content=r.question))
        turns.append(Turn(role="assistant", content=r.answer or ""))
    return turns


# ---- the account-wide key, set once by an admin ------------------------------
admin_router = APIRouter(prefix="/api/admin/assistant", tags=["admin"])


class SharedKeyStatus(BaseModel):
    configured: bool
    provider: str | None = None
    key_last_four: str | None = None
    updated_at: datetime | None = None
    daily_limit: int
    detail: str


class SharedKeyIn(BaseModel):
    provider: str = Field(pattern="^(anthropic|openai)$")
    api_key: str = Field(min_length=10, max_length=400)
    # Questions per user per day on this key. 0 turns the shared key off without
    # deleting it, which is the quickest way to stop spend during a surprise.
    daily_limit: int = Field(default=DEFAULT_DAILY_LIMIT, ge=0, le=1000)


def _explain(exc: Exception) -> HTTPException:
    """Turn an unexpected failure into something the person on the admin page can
    act on. A 500 in a browser console says only that something broke on a server
    whose logs they cannot see — which is where this whole feature stalled."""
    log.exception("admin assistant endpoint failed")
    return HTTPException(
        status_code=503,
        detail=f"Assistant settings are unavailable: {type(exc).__name__}: {exc}",
    )


def _shared_status(db: Session) -> SharedKeyStatus:
    provider = (settings_store.get(db, LLM_PROVIDER) or "").strip() or None
    stored = settings_store.get(db, LLM_API_KEY)
    limit = settings_store.daily_limit(db)
    if not provider or not stored:
        return SharedKeyStatus(
            configured=False, daily_limit=limit,
            detail="No account key set. Each user has to add their own key in "
                   "Settings before the assistant will answer.")
    if not keys.decrypt(stored):
        # Encrypted under a different ASSISTANT_KEY_SECRET than the one running.
        return SharedKeyStatus(
            configured=False, provider=provider, daily_limit=limit,
            detail="The saved key cannot be read with the current server secret. "
                   "Enter it again to replace it.")
    row = db.get(AppSetting, LLM_API_KEY)
    return SharedKeyStatus(
        configured=True, provider=provider, key_last_four=keys.last_four(stored),
        updated_at=row.updated_at if row else None, daily_limit=limit,
        detail=f"Connected to {provider}. Every user without their own key uses "
               f"this one, up to {limit} questions each per day.")


@admin_router.get("/key", response_model=SharedKeyStatus)
def get_shared_key(_: User = Depends(require_admin),
                   db: Session = Depends(get_db)) -> SharedKeyStatus:
    settings_store.ensure_table()
    try:
        return _shared_status(db)
    except Exception as exc:
        raise _explain(exc) from exc


@admin_router.put("/key", response_model=SharedKeyStatus)
def set_shared_key(body: SharedKeyIn, me: User = Depends(require_admin),
                   db: Session = Depends(get_db)) -> SharedKeyStatus:
    """Save the account-wide key. The key is verified against the provider before
    it is stored — a typo saved silently would surface later as every user's
    assistant being broken, with nothing to say why."""
    api_key = body.api_key.strip()
    if msg := keys.looks_valid(body.provider, api_key):
        raise HTTPException(status_code=422, detail=msg)
    try:
        providers.run(
            provider=body.provider, api_key=api_key,
            system="Reply with the single word: ok",
            messages=[{"role": "user", "content": "ok"}],
            specs=[], dispatch=lambda *_: "",
        )
    except providers.ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        settings_store.put(db, LLM_PROVIDER, body.provider, by=me.id)
        settings_store.put(db, LLM_API_KEY, keys.encrypt(api_key), by=me.id)
        settings_store.put(db, LLM_DAILY_LIMIT, str(body.daily_limit), by=me.id)
    except Exception as exc:
        raise _explain(exc) from exc
    log.warning("shared assistant key set by %s (provider=%s, limit=%d/day)",
                me.email, body.provider, body.daily_limit)
    return _shared_status(db)


class LimitIn(BaseModel):
    daily_limit: int = Field(ge=0, le=1000)


@admin_router.put("/limit", response_model=SharedKeyStatus)
def set_daily_limit(body: LimitIn, me: User = Depends(require_admin),
                    db: Session = Depends(get_db)) -> SharedKeyStatus:
    """Change the cap without re-entering the key."""
    try:
        settings_store.put(db, LLM_DAILY_LIMIT, str(body.daily_limit), by=me.id)
    except Exception as exc:
        raise _explain(exc) from exc
    log.warning("assistant daily limit set to %d by %s", body.daily_limit, me.email)
    return _shared_status(db)


@admin_router.delete("/key", response_model=SharedKeyStatus)
def clear_shared_key(me: User = Depends(require_admin),
                     db: Session = Depends(get_db)) -> SharedKeyStatus:
    try:
        settings_store.put(db, LLM_PROVIDER, None, by=me.id)
        settings_store.put(db, LLM_API_KEY, None, by=me.id)
    except Exception as exc:
        raise _explain(exc) from exc
    log.warning("shared assistant key cleared by %s", me.email)
    return _shared_status(db)


class UsageRow(BaseModel):
    user_id: int | None
    email: str | None
    used_today: int
    total: int


@admin_router.get("/usage", response_model=list[UsageRow])
def usage(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[UsageRow]:
    """Who is asking, and how much of today's allowance they have spent. This is
    the answer to "why is the bill what it is" without opening the provider's
    console."""
    from sqlalchemy import func as _f
    start = settings_store._day_start()
    try:
        totals = dict(db.query(AssistantLog.user_id, _f.count(AssistantLog.id))
                      .filter(AssistantLog.ok.is_(True))
                      .group_by(AssistantLog.user_id).all())
        today = dict(db.query(AssistantLog.user_id, _f.count(AssistantLog.id))
                     .filter(AssistantLog.ok.is_(True), AssistantLog.created_at >= start)
                     .group_by(AssistantLog.user_id).all())
        emails = dict(db.query(User.id, User.email).all())
        rows = [UsageRow(user_id=uid, email=emails.get(uid), used_today=today.get(uid, 0),
                         total=n)
                for uid, n in totals.items()]
    except Exception as exc:
        # Nobody has asked anything yet, or the log table is not there. Neither is
        # a server error, and answering 500 made the whole admin page look broken.
        db.rollback()
        log.warning("assistant usage unavailable: %s: %s", type(exc).__name__, exc)
        return []
    rows.sort(key=lambda r: (-r.used_today, -r.total))
    return rows
