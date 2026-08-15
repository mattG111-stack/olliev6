"""The bug log: report a fault, track it, export the lot as CSV.

Kept deliberately small. This is not an issue tracker — it is a record of what
broke, on which build, with what the server actually said, so a report survives
the gap between noticing a fault and someone sitting down to fix it.

Any signed-in user can file one; only an admin can read, edit or export the log.
Filing is open on purpose: the person who hits the fault is rarely the person
with the admin password, and a bug nobody can be bothered to report is a bug that
gets rediscovered three times.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..db import Base, engine, get_db
from ..models import BUG_SEVERITIES, BUG_SOURCES, BUG_STATUSES, BugReport, User
from ..security import current_user, require_admin
from ..version import VERSION

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bugs", tags=["bugs"])
admin_router = APIRouter(prefix="/api/admin/bugs", tags=["admin"])


def _ensure_table() -> None:
    """Create bug_reports if it is missing.

    Same reasoning as the settings store: db_bootstrap's create_all is wrapped in
    a catch-all, so a failure there would make this whole feature 500 with the
    cause buried in a boot log. A table that reports faults must not become one.
    """
    try:
        BugReport.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        log.exception("could not create bug_reports")


def record(db: Session, *, title: str, detail: str | None, source: str,
           fingerprint: str, severity: str = "high", page: str | None = None,
           app_version: str | None = None, user_agent: str | None = None,
           reporter: User | None = None, api_errors: list | None = None) -> BugReport | None:
    """File a report, or bump the one that is already there.

    Automatic capture without de-duplication is a log that floods and therefore a
    log nobody opens: one broken endpoint clicked ten times is one fault, not ten
    entries. Repeats of the same fingerprint that are still open are counted and
    re-stamped instead.

    Never raises. This runs inside an error handler — a failure to record a fault
    must not become a second fault, and must never replace the original error the
    caller was already dealing with.
    """
    try:
        _ensure_table()
        existing = (db.query(BugReport)
                    .filter(BugReport.fingerprint == fingerprint,
                            BugReport.status == "open")
                    .order_by(desc(BugReport.created_at)).first())
        now = datetime.now(timezone.utc)
        if existing is not None:
            existing.occurrences = (existing.occurrences or 1) + 1
            existing.last_seen_at = now
            # A fault that keeps happening on a newer build is still happening.
            existing.api_version = VERSION
            db.commit()
            return existing
        b = BugReport(
            reported_by_id=getattr(reporter, "id", None),
            reported_by_email=getattr(reporter, "email", None),
            title=title[:300], detail=detail, page=page, severity=severity,
            status="open", source=source, fingerprint=fingerprint[:200],
            occurrences=1, last_seen_at=now,
            app_version=app_version, api_version=VERSION,
            user_agent=(user_agent or None),
            api_errors_json=json.dumps(api_errors or []),
        )
        db.add(b); db.commit(); db.refresh(b)
        return b
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        log.exception("could not record bug automatically")
        return None


class ApiErrorIn(BaseModel):
    """One failed request the browser saw, as the browser saw it."""
    at: str | None = None
    path: str | None = None
    status: int | None = None
    detail: str | None = None


class BugIn(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    detail: str | None = Field(default=None, max_length=8000)
    page: str | None = Field(default=None, max_length=500)
    severity: str = Field(default="normal")
    app_version: str | None = Field(default=None, max_length=32)
    # Captured by the form, not typed by the reporter.
    api_errors: list[ApiErrorIn] = Field(default_factory=list, max_length=20)


class BugOut(BaseModel):
    id: int
    created_at: datetime
    reported_by_email: str | None
    title: str
    detail: str | None
    page: str | None
    severity: str
    status: str
    resolution: str | None
    resolved_at: datetime | None
    app_version: str | None
    api_version: str | None
    user_agent: str | None
    source: str = "manual"
    occurrences: int = 1
    last_seen_at: datetime | None = None
    api_errors: list[ApiErrorIn] = []

    class Config:
        from_attributes = True


def _out(b: BugReport) -> BugOut:
    try:
        errs = json.loads(b.api_errors_json) if b.api_errors_json else []
    except Exception:
        errs = []
    return BugOut(
        id=b.id, created_at=b.created_at, reported_by_email=b.reported_by_email,
        title=b.title, detail=b.detail, page=b.page, severity=b.severity,
        status=b.status, resolution=b.resolution, resolved_at=b.resolved_at,
        app_version=b.app_version, api_version=b.api_version,
        user_agent=b.user_agent, source=getattr(b, "source", "manual") or "manual",
        occurrences=getattr(b, "occurrences", 1) or 1,
        last_seen_at=getattr(b, "last_seen_at", None),
        api_errors=[ApiErrorIn(**e) for e in errs if isinstance(e, dict)],
    )


@router.post("", response_model=BugOut, status_code=201)
def report_bug(body: BugIn, request: Request, me: User = Depends(current_user),
               db: Session = Depends(get_db)) -> BugOut:
    """File a bug. Any signed-in user."""
    _ensure_table()
    if body.severity not in BUG_SEVERITIES:
        raise HTTPException(status_code=400, detail=f"Severity must be one of {BUG_SEVERITIES}")
    b = BugReport(
        reported_by_id=me.id, reported_by_email=me.email,
        title=body.title.strip(), detail=(body.detail or "").strip() or None,
        page=body.page, severity=body.severity, status="open",
        app_version=body.app_version,
        # The API version is taken from the server answering, never from the
        # client — the mismatch between the two is itself a common cause, and a
        # report that trusted the browser for both could not show it.
        api_version=VERSION,
        user_agent=(request.headers.get("user-agent") or "")[:500] or None,
        api_errors_json=json.dumps([e.model_dump() for e in body.api_errors]) or None,
    )
    try:
        db.add(b); db.commit(); db.refresh(b)
    except Exception as exc:
        db.rollback()
        log.exception("could not save bug report")
        raise HTTPException(status_code=503,
                            detail=f"Could not save the report: {type(exc).__name__}: {exc}") from exc
    log.warning("BUG REPORTED by %s [%s] %s", me.email, b.severity, b.title)
    return _out(b)


class ClientErrorIn(BaseModel):
    """A crash in the page, as the browser saw it."""
    message: str = Field(min_length=1, max_length=500)
    stack: str | None = Field(default=None, max_length=8000)
    page: str | None = Field(default=None, max_length=500)
    app_version: str | None = Field(default=None, max_length=32)


@router.post("/client", status_code=202)
def report_client_error(body: ClientErrorIn, request: Request,
                        me: User = Depends(current_user),
                        db: Session = Depends(get_db)) -> dict:
    """A crash in the browser, filed by the page itself.

    Every round of this has started with a console error pasted into a chat. The
    page can send it directly, with the build it happened on attached — the same
    information, without anyone having to notice, copy and describe it.

    202, not 201: the report is accepted and de-duplicated, and the browser has
    nothing useful to do with the outcome either way.
    """
    # Fingerprint on the message and the page, not the stack — minified builds
    # give a different stack per deploy for the same fault, which would file the
    # same crash again after every release.
    fp = f"browser|{(body.page or '')}|{body.message[:120]}"
    b = record(
        db, title=f"[browser] {body.message[:200]}",
        detail=body.stack, source="browser", fingerprint=fp, severity="high",
        page=body.page, app_version=body.app_version,
        user_agent=(request.headers.get("user-agent") or "")[:500] or None,
        reporter=me,
    )
    return {"recorded": b is not None, "id": getattr(b, "id", None),
            "occurrences": getattr(b, "occurrences", None)}


@admin_router.get("", response_model=list[BugOut])
def list_bugs(status: str | None = Query(None), _: User = Depends(require_admin),
              db: Session = Depends(get_db)) -> list[BugOut]:
    _ensure_table()
    q = db.query(BugReport).order_by(desc(BugReport.created_at))
    if status:
        q = q.filter(BugReport.status == status)
    return [_out(b) for b in q.limit(500).all()]


class BugPatch(BaseModel):
    status: str | None = None
    severity: str | None = None
    resolution: str | None = Field(default=None, max_length=8000)


@admin_router.patch("/{bug_id}", response_model=BugOut)
def update_bug(bug_id: int, body: BugPatch, _: User = Depends(require_admin),
               db: Session = Depends(get_db)) -> BugOut:
    b = db.get(BugReport, bug_id)
    if not b:
        raise HTTPException(status_code=404, detail="Bug not found")
    if body.status is not None:
        if body.status not in BUG_STATUSES:
            raise HTTPException(status_code=400, detail=f"Status must be one of {BUG_STATUSES}")
        b.status = body.status
        # Stamp when it stopped being open, so "how long was this broken" is
        # answerable later without reading the whole history.
        b.resolved_at = datetime.now(timezone.utc) if body.status != "open" else None
    if body.severity is not None:
        if body.severity not in BUG_SEVERITIES:
            raise HTTPException(status_code=400, detail=f"Severity must be one of {BUG_SEVERITIES}")
        b.severity = body.severity
    if body.resolution is not None:
        b.resolution = body.resolution.strip() or None
    db.commit(); db.refresh(b)
    return _out(b)


@admin_router.delete("/{bug_id}", status_code=204)
def delete_bug(bug_id: int, _: User = Depends(require_admin),
               db: Session = Depends(get_db)):
    from fastapi import Response

    b = db.get(BugReport, bug_id)
    if not b:
        raise HTTPException(status_code=404, detail="Bug not found")
    db.delete(b); db.commit()
    return Response(status_code=204)


@admin_router.get("/export.csv")
def export_bugs_csv(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    """The whole log as a CSV — one row per bug, the captured API errors flattened
    into a single readable column so the file is useful in Excel rather than being
    a wall of JSON."""
    _ensure_table()
    cols = ["id", "created_at", "last_seen_at", "occurrences", "source", "status",
            "severity", "title", "detail", "page", "reported_by", "app_version",
            "api_version", "api_errors", "resolution", "resolved_at", "user_agent"]

    def rows():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for b in db.query(BugReport).order_by(desc(BugReport.created_at)).limit(5000):
            o = _out(b)
            errs = " | ".join(
                f"{e.status or '?'} {e.path or '?'}: {(e.detail or '')[:200]}"
                for e in o.api_errors
            )
            w.writerow([
                b.id, b.created_at.isoformat() if b.created_at else "",
                b.last_seen_at.isoformat() if b.last_seen_at else "",
                b.occurrences or 1, getattr(b, "source", "manual"), b.status,
                b.severity, b.title, b.detail or "", b.page or "",
                b.reported_by_email or "", b.app_version or "", b.api_version or "",
                errs, b.resolution or "",
                b.resolved_at.isoformat() if b.resolved_at else "", b.user_agent or "",
            ])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    return StreamingResponse(
        rows(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="apex-bugs.csv"'},
    )
