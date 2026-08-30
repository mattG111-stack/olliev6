"""Ask-anything endpoint, plus the Settings screen's key management.

Answers are grounded in tool calls against real data. The user brings their own
Claude or OpenAI key; it is stored encrypted and never returned to the client.
"""

from __future__ import annotations

import json
import re
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..assistant import keys, providers
from ..assistant.agent import AssistantUnavailable, Turn, ask
from ..db import SessionLocal, get_db
from ..preferences import _aware
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
    # Everything anyone has asked and had answered is available to draw on, not
    # just this account's own history. Best-effort in both directions: a lookup
    # that fails must not cost the reader their answer.
    try:
        learned = _similar_answered(db, body.question, exclude_user=me.id)
    except Exception:
        log.exception("could not read prior questions; answering without them")
        learned = []
    turns = learned + turns

    # The daily cap, enforced only on the account-wide key. Someone using their
    # own key is spending their own money and is not counted.
    # Outside the try below, so a failure here used to 500 as well. A quota that
    # cannot be read is not a reason to refuse the question — worst case someone
    # asks one more than their allowance.
    try:
        quota = settings_store.quota_for(db, me)
    except Exception:
        log.exception("could not read the assistant quota; allowing the question")
        quota = {"shared": False, "configured": True, "remaining": None, "limit": None}
    if quota["shared"] and quota["configured"] and (quota["remaining"] or 0) <= 0:
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
    except Exception as exc:
        # Anything else. This branch is why "[browser] 500 from /api/assistant"
        # has been reported since v1.8 and never diagnosed: only the two
        # exception types above were handled, so every other failure — a tool
        # raising, a query the model wrote that the database rejected, a shape
        # the parser did not expect — became a bare 500 with no server-side log
        # and no detail. Nothing recorded what broke, so each report carried the
        # same single fact: that something did.
        log.exception("assistant question failed: %r", body.question[:200])
        try:
            _log_question(db, me.id, body.question,
                          answer=f"{type(exc).__name__}: {exc}"[:2000],
                          ok=False, tools=None)
        except Exception:
            pass    # the log is not worth losing the real error over
        raise HTTPException(
            status_code=500,
            detail=f"Ollie could not answer that: {type(exc).__name__}: {exc}",
        ) from exc

    # Store every question + answer — the corpus Ollie learns from.
    _log_question(db, me.id, body.question, answer=result.text, ok=True,
                  tools=result.tools_used)
    return AskOut(answer=result.text, tools_used=result.tools_used,
                  iterations=result.iterations, queries=result.queries)


# --- asking without a clock --------------------------------------------------
# The endpoint above answers inside the request, which means the answer has to
# beat whatever proxy sits in front of this service. It does that by giving up
# at 55 seconds and saying so — an honest answer to the wrong question.
#
#     "a user askes a question then ollie does not time out till it answers it
#      you could even count in a small corner 0-100% till your answer"
#
# So asking becomes a job, the same shape as the portal sweep and the gap fill.
# POST /ask writes the question down and returns an id in milliseconds; a worker
# thread answers it with no deadline at all; GET /ask/{id} reports how far along
# it is. Nothing holds a socket open, so nothing can cut one.

# The highest a question still in flight may report. It is not 99, because a
# number that sits at 99 for a minute is a lie people recognise; 100 means the
# answer is on the screen and nothing else does.
ASK_CEILING = 92
# How long one step is assumed to take when easing between milestones. Only
# affects the number BETWEEN real events, and never carries it to the next one.
CREEP_SECONDS = 45.0
# After this long with no sign of life, a question that still says "running"
# has no worker behind it and never will.
#
# THE HOLE IN "IT NEVER TIMES OUT". A worker thread dies with its container — a
# redeploy, an OOM — and nothing is left to finish the row. Without this it
# stays "running" for ever and the browser polls it for ever, counter creeping
# toward a milestone that will never arrive. That is not taking its time; it is
# a hang wearing patience as a disguise, and it is worse than the timeout it
# replaced, because there is no end to it at all.
#
# Comfortably beyond the longest a single step can legitimately take: one model
# call may run for CALL_TIMEOUT_UNBOUNDED (five minutes) and tool dispatch runs
# after it, and every step stamps phase_at. Fifteen minutes is roughly triple
# that, so a question that is genuinely working is never mistaken for a dead
# one — which is the failure that would matter more.
ASK_STALE_MINUTES = 15
# Per turn of client-sent history kept on the row. Twenty turns at this size is
# a bounded row; uncapped, it is whatever the caller felt like sending.
HISTORY_CHARS = 4000
_ABANDONED = ("Ollie was interrupted before he could finish that one — the "
              "server restarted while he was working. Ask it again and he'll "
              "start it fresh.")
# What each phase is called for the person waiting. Deliberately about their
# question rather than about our machinery — no tool names, no model names.
_PHASES = {
    "thinking": "Thinking it through",
    "tool": "Looking up the data",
    "writing": "Writing the answer",
}


def progress_for(rounds: int) -> int:
    """0-100 for a question that has completed `rounds` steps of work.

    Real milestones, not a timer dressed up as a bar. A round is one completed
    pass of the model — think, call tools, read them back — so this genuinely
    rises when work is done and stays put when nothing is happening.

    The total number of rounds cannot be known in advance (that is the whole
    nature of the thing: the model decides how many lookups a question needs),
    so this closes 45% of the remaining distance to ASK_CEILING each round.
    Monotonic, never overshoots, and never reaches 100 — only an answer does
    that.
    """
    if rounds <= 0:
        return 4                      # asked, accepted, not yet started
    return min(ASK_CEILING, round(ASK_CEILING * (1 - 0.55 ** rounds)))


def shown_progress(row: AssistantLog, now: datetime) -> int:
    """What the corner of the screen should read, right now.

    Milestones alone leave the number frozen through a step that can take a
    minute, and a number that does not move reads as a hang — the same lesson
    the fill job's row counter taught. So between milestones it eases toward
    the next one on elapsed time, which is a real signal, and is capped a
    point short so it can never claim work that has not happened.
    """
    if (row.status or "done") != "running":
        return 100
    rounds = row.iterations or 0
    base, nxt = progress_for(rounds), progress_for(rounds + 1)
    at = _aware(row.phase_at)
    secs = (now - at).total_seconds() if at else 0.0
    frac = min(0.9, max(0.0, secs / CREEP_SECONDS))
    return max(base, min(nxt - 1, int(base + (nxt - base) * frac)))


def _reap_if_abandoned(db: Session, row: AssistantLog, now: datetime) -> bool:
    """Release a question whose worker died. Returns True if it just did.

    Read-time rather than a sweeper: the only person who cares is the one
    waiting on it, and they are already polling. That also makes it
    self-correcting — a row nobody is watching costs nothing by sitting there,
    and the moment anybody looks, it is resolved.

    Idempotent: _finish only touches a row that is still "running", so a second
    look reads the failure that the first one wrote rather than stamping a new
    one over it.
    """
    if (row.status or "done") != "running":
        return False
    beat = _aware(row.phase_at) or _aware(row.started_at) or _aware(row.created_at)
    if beat is None or (now - beat) < timedelta(minutes=ASK_STALE_MINUTES):
        return False
    log.warning("ask %s abandoned — no progress since %s", row.id, beat)
    _finish(db, row.id, ok=False, answer=_ABANDONED, status="failed")
    # update(synchronize_session=False) does not touch the object already in
    # this session, so without a refresh the caller reads the stale "running".
    try:
        db.refresh(row)
    except Exception:                             # noqa: BLE001
        pass
    return True


class AskStarted(BaseModel):
    """The answer to POST /ask — always immediate."""
    ask_id: int
    status: str = "running"


class AskProgress(BaseModel):
    ask_id: int
    status: str                  # running | done | failed
    progress_pct: int            # 0-100
    phase: str | None            # what it is doing, in plain words
    elapsed_seconds: int
    # Present only once status is "done".
    answer: str | None = None
    tools_used: list[str] = []
    queries: list[str] = []
    iterations: int = 0
    error: str | None = None     # present only when status is "failed"


def _run_ask_job(ask_id: int, user_id: int) -> None:
    """Answer one question, on its own thread, for as long as it takes.

    NOTHING IS WAITING ON THIS. That is the whole point: the request that
    started it came back with an id straight away, so there is no proxy patience
    to beat, no 55-second give-up, and no connection to be cut mid-answer. A
    question that needs six minutes and nine lookups gets six minutes and nine
    lookups.

    Never raises. A background thread that dies takes its reason with it, and
    this row is the only place the asker — or an admin reading the failure list
    — can find out what happened.
    """
    db = SessionLocal()
    started = time.monotonic()
    try:
        row = db.get(AssistantLog, ask_id)
        me = db.get(User, user_id) if user_id else None
        if row is None or me is None:
            return
        if (row.status or "done") != "running":
            # Stand down WITHOUT writing. Any status other than "running" means
            # somebody else has already finished, failed or abandoned this ask,
            # and stamping over it would erase a real answer — the same trap the
            # fill worker has a guard for.
            log.info("ask %s is no longer ours — standing down", ask_id)
            return

        # Context is gathered here rather than in the request so that the
        # request stays instant. Both lookups are best-effort in both
        # directions: neither is worth losing an answer over.
        turns: list[Turn] = _stored_turns(row)
        if not turns:
            try:
                turns = _recent_memory(db, user_id)
            except Exception:                     # noqa: BLE001
                turns = []
        try:
            learned = _similar_answered(db, row.question, exclude_user=user_id)
        except Exception:                         # noqa: BLE001
            log.exception("could not read prior questions; answering without them")
            learned = []
        turns = learned + turns

        rounds = {"n": 0}

        def on_step(kind: str, detail: str) -> None:
            """Called from inside the model loop as real work completes.

            A round is counted on "thinking" — the START of a pass — so the
            number moves as soon as something is under way rather than only
            once a pass has finished. Writes are best-effort by contract:
            providers._note swallows anything raised here, so a database
            hiccup costs a progress update and never the answer.
            """
            if kind == "thinking":
                rounds["n"] += 1
            _mark(db, ask_id, phase=_PHASES.get(kind, "Working on it"),
                  progress_pct=progress_for(rounds["n"]), iterations=rounds["n"])

        result = ask(me, row.question, turns,
                     shared=settings_store.shared_key(db),
                     deadline=None,                     # take as long as it takes
                     max_iterations=providers.JOB_MAX_ITERATIONS,
                     on_step=on_step)

        if not (result.text or "").strip():
            # A model can come back with no text at all. Stored as an answer
            # that is a blank, the page renders an empty bubble, which reads as
            # a broken product rather than as a question that did not land.
            _finish(db, ask_id, ok=False, status="failed",
                    answer="Ollie came back with nothing on that one. Ask it "
                           "again, or put it a different way.")
            return

        _finish(db, ask_id, ok=True, answer=result.text,
                tools=result.tools_used, queries=result.queries,
                iterations=result.iterations)
    except AssistantUnavailable as exc:
        _finish(db, ask_id, ok=False, answer=str(exc), status="failed")
    except providers.ProviderError as exc:
        _finish(db, ask_id, ok=False, answer=str(exc), status="failed")
    except Exception as exc:                      # noqa: BLE001
        log.exception("assistant job %s failed", ask_id)
        _finish(db, ask_id, ok=False, status="failed",
                answer=f"{type(exc).__name__}: {exc}"[:2000])
    finally:
        log.info("ask %s finished in %.1fs", ask_id, time.monotonic() - started)
        db.close()


def _mark(db: Session, ask_id: int, **fields) -> None:
    """Move a running ask forward. Never touches one that has stopped.

    The status filter is the guard: a late progress write arriving after the
    answer landed would otherwise put the row back into "running" and leave the
    asker's screen counting toward an answer that is already there.
    """
    try:
        fields["phase_at"] = datetime.now(timezone.utc)
        (db.query(AssistantLog)
           .filter(AssistantLog.id == ask_id, AssistantLog.status == "running")
           .update(fields, synchronize_session=False))
        db.commit()
    except Exception:                             # noqa: BLE001
        db.rollback()


def _finish(db: Session, ask_id: int, *, ok: bool, answer: str | None,
            tools: list[str] | None = None, queries: list[str] | None = None,
            iterations: int = 0, status: str = "done") -> None:
    """Write the result. The one write that must land."""
    try:
        now = datetime.now(timezone.utc)
        (db.query(AssistantLog)
           .filter(AssistantLog.id == ask_id, AssistantLog.status == "running")
           .update({
               "status": status, "ok": ok,
               "answer": (answer or "")[:16000] or None,
               "tools_used": json.dumps(tools) if tools else None,
               "queries": json.dumps(queries) if queries else None,
               "iterations": iterations,
               "progress_pct": 100, "phase": None, "finished_at": now,
           }, synchronize_session=False))
        db.commit()
    except Exception:                             # noqa: BLE001
        log.exception("could not record the answer for ask %s", ask_id)
        db.rollback()


def _stored_turns(row: AssistantLog) -> list[Turn]:
    """The history the client sent with the question, parked on the row.

    It has to survive the hand-off to the worker thread, and the row is the
    only thing both sides can see. Stored on `queries` while the ask is running
    and overwritten with the real SQL when it finishes — one column, two lives,
    which is worth it against a migration for a field that exists for seconds.
    """
    try:
        raw = json.loads(row.queries) if row.queries else []
    except (ValueError, TypeError):
        return []
    return [Turn(role=t["role"], content=t["content"])
            for t in raw if isinstance(t, dict) and t.get("role") and t.get("content")]


@router.post("/ask", response_model=AskStarted)
def start_ask(body: AskIn, me: User = Depends(require_active),
              db: Session = Depends(get_db)) -> AskStarted:
    """Take the question and come straight back with a ticket for it.

    The checks that must be answered NOW — no key, no allowance left — happen
    here, so those still arrive as a 428 or a 429 the moment the button is
    pressed rather than after a poll. Everything that takes time happens on the
    thread.
    """
    # Same two gates as the synchronous endpoint, in the same order, so the two
    # doors cannot start disagreeing about who may ask a question.
    provider, shared_key = settings_store.shared_key(db)
    if not ((me.llm_provider and keys.decrypt(me.llm_api_key_encrypted))
            or (provider and shared_key)):
        raise HTTPException(
            status_code=428,
            detail="The assistant is not connected yet. An admin can add an API "
                   "key in the admin panel, or you can add your own in Settings.")
    try:
        quota = settings_store.quota_for(db, me)
    except Exception:                             # noqa: BLE001
        log.exception("could not read the assistant quota; allowing the question")
        quota = {"shared": False, "configured": True, "remaining": None, "limit": None}
    if quota["shared"] and quota["configured"] and (quota["remaining"] or 0) <= 0:
        raise HTTPException(
            status_code=429,
            detail=f"You have used your {quota['limit']} questions for today. "
                   f"The allowance resets at midnight. Add your own API key in "
                   f"Settings for unlimited questions.")

    now = datetime.now(timezone.utc)
    row = AssistantLog(
        user_id=me.id, question=body.question[:8000], answer=None, ok=True,
        status="running", progress_pct=progress_for(0), phase="Getting started",
        phase_at=now, started_at=now,
        # History is CONTEXT, not data — and it arrives from the client, which
        # means its size is not ours to assume. The question itself is capped at
        # 2000 characters and this was capped at nothing, so twenty turns of
        # arbitrary length went into the row verbatim. Truncating rather than
        # refusing: a long prior answer is still useful context at 4000
        # characters, and 422-ing somebody's conversation because it got long is
        # a worse outcome than trimming it.
        queries=json.dumps([{"role": t.role, "content": t.content[:HISTORY_CHARS]}
                            for t in body.history]) or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    threading.Thread(target=_run_ask_job, args=(row.id, me.id),
                     daemon=True).start()
    return AskStarted(ask_id=row.id)


@router.get("/ask/{ask_id}", response_model=AskProgress)
def read_ask(ask_id: int, me: User = Depends(require_active),
             db: Session = Depends(get_db)) -> AskProgress:
    """How far along, and the answer once there is one.

    A question and its answer are the asker's own — what they are looking at and
    what they can afford is in there — so only they can read it back.
    """
    row = db.get(AssistantLog, ask_id)
    if row is None or (row.user_id != me.id and (me.role or "") != "admin"):
        raise HTTPException(status_code=404, detail="No such question.")

    now = datetime.now(timezone.utc)
    # A question whose worker died with its container would otherwise say
    # "running" for ever, and be polled for ever.
    _reap_if_abandoned(db, row, now)
    status = row.status or "done"
    started = _aware(row.started_at) or _aware(row.created_at) or now
    finished = _aware(row.finished_at) or now
    done = status != "running"

    tools, queries = [], []
    if done:
        try:
            tools = json.loads(row.tools_used) if row.tools_used else []
        except (ValueError, TypeError):
            tools = []
        try:
            queries = json.loads(row.queries) if row.queries else []
        except (ValueError, TypeError):
            queries = []
        # While it was running this column held the client's history, which is
        # not SQL and must never be shown as though it were.
        if not all(isinstance(q, str) for q in queries):
            queries = []

    failed = status == "failed"
    return AskProgress(
        ask_id=row.id, status=status,
        progress_pct=shown_progress(row, now),
        phase=row.phase if not done else None,
        elapsed_seconds=int(max(0.0, ((finished if done else now) - started).total_seconds())),
        answer=None if failed else (row.answer if done else None),
        tools_used=tools, queries=queries, iterations=row.iterations or 0,
        error=row.answer if failed else None,
    )


class QuotaOut(BaseModel):
    """What the Ask page shows above the box, so nobody meets the cap by surprise."""
    shared: bool                 # using the account key rather than their own
    configured: bool             # a usable key exists at all
    limit: int | None            # None = unlimited (their own key)
    used: int
    remaining: int | None


class FailureRow(BaseModel):
    """One question Ollie could not answer, and what actually went wrong."""
    id: int
    at: str
    email: str | None
    question: str
    error: str | None


@router.get("/failures", response_model=list[FailureRow])
def recent_failures(limit: int = 20, _: User = Depends(require_admin),
                    db: Session = Depends(get_db)) -> list[FailureRow]:
    """The last questions that failed, with the recorded reason.

    Every failure has been written to assistant_logs with the exception on it
    since the catch-all went in — and there has been nowhere to read it. So the
    bug reports kept saying "500 from /api/assistant: HTTP 500", the one fact
    that cannot be acted on, while the actual cause sat in a table.
    """
    rows = (db.query(AssistantLog, User.email)
            .outerjoin(User, User.id == AssistantLog.user_id)
            .filter(AssistantLog.ok.is_(False))
            .order_by(AssistantLog.id.desc())
            .limit(max(1, min(limit, 100)))
            .all())
    return [
        FailureRow(
            id=log.id,
            at=log.created_at.isoformat() if log.created_at else "",
            email=email,
            question=(log.question or "")[:300],
            # The answer column carries "TypeError: ..." for a failed row.
            error=(log.answer or None),
        )
        for log, email in rows
    ]


class QuestionRow(BaseModel):
    """One question, and who asked it. Admin eyes only."""
    id: int
    at: str
    email: str | None
    name: str | None
    question: str
    answer: str | None
    ok: bool
    tools: list[str] = []


@router.get("/questions", response_model=list[QuestionRow])
def all_questions(limit: int = 100, search: str | None = None,
                  failures_only: bool = False,
                  _: User = Depends(require_admin),
                  db: Session = Depends(get_db)) -> list[QuestionRow]:
    """Every question anyone has asked, newest first.

    Admin-only, deliberately: the questions carry what a customer is looking at
    and what they are worth, which is theirs. It is on the admin dashboard so
    the account owner can see what the product is being used for.
    """
    q = (db.query(AssistantLog, User.email, User.full_name)
         .outerjoin(User, User.id == AssistantLog.user_id))
    if failures_only:
        q = q.filter(AssistantLog.ok.is_(False))
    if search:
        like = f"%{search.strip()}%"
        q = q.filter(or_(AssistantLog.question.ilike(like),
                         AssistantLog.answer.ilike(like),
                         User.email.ilike(like)))
    rows = q.order_by(AssistantLog.id.desc()).limit(max(1, min(limit, 500))).all()

    out: list[QuestionRow] = []
    for log_row, email, name in rows:
        try:
            tools = json.loads(log_row.tools_used) if log_row.tools_used else []
        except (ValueError, TypeError):
            tools = []
        out.append(QuestionRow(
            id=log_row.id,
            at=log_row.created_at.isoformat() if log_row.created_at else "",
            email=email, name=name,
            question=(log_row.question or "")[:2000],
            answer=(log_row.answer or None),
            ok=bool(log_row.ok),
            tools=[str(t) for t in tools][:12],
        ))
    return out


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


# Words that carry no signal about what a question is ABOUT, so matching on them
# would make every question look similar to every other one.
_STOPWORDS = {
    "what", "which", "where", "when", "who", "how", "why", "is", "are", "was",
    "were", "the", "a", "an", "in", "on", "at", "of", "for", "to", "and", "or",
    "me", "my", "i", "we", "you", "it", "this", "that", "with", "about", "any",
    "show", "tell", "give", "find", "list", "can", "do", "does", "did", "has",
    "have", "there", "best", "most", "should",
}


def _keywords(text_: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']{3,}", (text_ or "").lower())
            if w not in _STOPWORDS}


def _similar_answered(db: Session, question: str, *, exclude_user: int | None,
                      limit: int = 3, scan: int = 400) -> list[Turn]:
    """Questions like this one that were answered well — from EVERYONE.

    The assistant used to see only the asker's own last three exchanges, so
    every account started from nothing and the same question was worked out from
    scratch over and over. The log holds every question anyone has asked and
    what came back; this is what makes that a corpus rather than an archive.

    Matched on shared keywords rather than embeddings — no extra service to run,
    and on questions of this shape ("what's happening in Remuera", "most
    underpriced in Browns Bay") the suburb and the subject ARE the keywords.

    Only answered questions are offered, and the asker's own recent turns are
    handled separately, so their own history is not duplicated here.
    """
    want = _keywords(question)
    if not want:
        return []
    rows = (db.query(AssistantLog)
            .filter(AssistantLog.ok.is_(True), AssistantLog.answer.isnot(None))
            .order_by(AssistantLog.id.desc()).limit(scan).all())
    scored: list[tuple[float, int, AssistantLog]] = []
    for r in rows:
        if exclude_user is not None and r.user_id == exclude_user:
            continue
        have = _keywords(r.question or "")
        if not have:
            continue
        overlap = len(want & have)
        if overlap < 2:                     # one word in common is a coincidence
            continue
        # Jaccard, so a long rambling question does not beat a precise one just
        # by containing more words.
        score = overlap / len(want | have)
        scored.append((score, r.id, r))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    turns: list[Turn] = []
    for _score, _id, r in scored[:limit]:
        turns.append(Turn(role="user", content=r.question))
        turns.append(Turn(role="assistant", content=(r.answer or "")[:4000]))
    return turns


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
