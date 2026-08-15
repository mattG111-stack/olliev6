import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse, Response

from .config import settings
from .db import SessionLocal, engine
from .routers import (admin_metrics, admin_upload, assistant, auth, billing, bugs,
                      dashboards, geo, properties, release, wishlists)
from .security import ensure_seed_admin, require_active
from .security import require_admin as require_admin_dep
from .db import get_db as get_db_dep
from .version import BUILT_AT, VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    log = logging.getLogger(__name__)
    log.warning("Apex Property API v%s (built %s) starting", VERSION, BUILT_AT)
    # Create any table the models define that the database lacks.
    #
    # The Procfile runs db_bootstrap before uvicorn, which is where this was
    # supposed to happen — but the production boot log contains no trace of it,
    # so the start command in use is not the Procfile's. The result is that
    # EVERY table added after the original schema is missing: assistant_logs,
    # app_settings, the geo tables, bug_reports. That single fact is behind the
    # geo 500s, the assistant 500s, and the empty assistant usage table, each of
    # which was diagnosed separately as its own bug.
    #
    # Doing it here as well makes the schema depend on the application starting
    # rather than on a start command nobody can see. create_all never drops or
    # alters anything that exists, so it is safe on a populated database.
    try:
        from sqlalchemy import inspect as _inspect

        from .db import Base as _Base

        before = set(_inspect(engine).get_table_names())
        _Base.metadata.create_all(engine)
        created = sorted(set(_inspect(engine).get_table_names()) - before)
        if created:
            log.warning("created %d missing table(s): %s", len(created), ", ".join(created))
    except Exception:
        # Never abort startup for this. A running app that is missing a table
        # beats no app, and every feature that needs one now creates it on demand.
        log.exception("could not ensure the schema")

    db = SessionLocal()
    try:
        ensure_seed_admin(db)
    finally:
        db.close()
    # Self-heal any staged batch still holding pre-guard CV-over valuations, in a
    # background daemon thread so a deploy fixes the stored numbers on its own —
    # no operator has to remember to click "Price". Backgrounded (never awaited)
    # so it can't delay startup or block the /health check, and it no-ops fast
    # when nothing is stale.
    import threading

    from .staged_stages import auto_reprice_stale_batches
    threading.Thread(target=auto_reprice_stale_batches, daemon=True).start()
    yield


app = FastAPI(
    title="Apex Property API",
    version=VERSION,
    description="Backend for the Apex Property platform.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The product data itself is paywalled: no free access. These routers require an
# active subscription (or admin / admin-approved account) — an authenticated but
# not-yet-subscribed user gets 402 and the frontend routes them to onboarding.
PAYWALL = [Depends(require_active)]

app.include_router(auth.router)
app.include_router(auth.admin_router)
app.include_router(properties.router, dependencies=PAYWALL)
app.include_router(assistant.router, dependencies=PAYWALL)
app.include_router(assistant.admin_router)
# Reporting a fault must work even when the product is paywalled off — the
# person who cannot get in is exactly the person with something to report.
app.include_router(bugs.router)
app.include_router(bugs.admin_router)
app.include_router(properties.sold_router, dependencies=PAYWALL)
app.include_router(dashboards.router, dependencies=PAYWALL)
app.include_router(admin_upload.router)
app.include_router(admin_metrics.router)
app.include_router(release.router)
app.include_router(billing.router)
app.include_router(geo.router, dependencies=PAYWALL)
app.include_router(wishlists.router, dependencies=PAYWALL)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": VERSION}


@app.exception_handler(StarletteHTTPException)
async def _record_http_error(request: Request, exc: StarletteHTTPException):
    """Record the deliberate failures too, not just the crashes.

    The previous handler only saw UNHANDLED exceptions. Every error this codebase
    raises on purpose — "assistant settings are unavailable: UndefinedTable",
    "could not delete: FOREIGN KEY constraint failed" — is an HTTPException, which
    FastAPI handles, so none of them reached the log. Those are the most useful
    ones: someone already worked out what went wrong and wrote it down.

    5xx only. A 401 on an expired token, a 404 on a stale link and a 422 on a
    mistyped form are the application working, and filing them would bury the
    real faults.
    """
    if exc.status_code >= 500:
        _record_bug(
            request,
            title=f"[server] {exc.status_code} on {request.method} {request.url.path}",
            detail=f"{request.method} {request.url.path}\n\n{exc.detail}",
            fingerprint=f"http|{exc.status_code}|{request.url.path}|{str(exc.detail)[:80]}",
            severity="blocker" if exc.status_code >= 500 else "high",
            status=exc.status_code, message=str(exc.detail),
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


def _record_bug(request: Request, *, title: str, detail: str, fingerprint: str,
                severity: str, status: int, message: str) -> None:
    """Shared recorder for both handlers. Never raises, never changes the response."""
    # Never file a fault about the machinery that files faults — that is an
    # unbounded loop with the log as its output.
    if request.url.path.startswith("/api/bugs"):
        return
    try:
        from .db import SessionLocal
        from .routers import bugs as _bugs

        db = SessionLocal()
        try:
            _bugs.record(
                db, title=title, detail=detail[:8000], source="server",
                fingerprint=fingerprint, severity=severity,
                page=str(request.url.path),
                user_agent=(request.headers.get("user-agent") or "")[:500] or None,
                api_errors=[{"path": request.url.path, "status": status,
                             "detail": message[:500]}],
            )
        finally:
            db.close()
    except Exception:                    # pragma: no cover - never mask the original
        logging.getLogger(__name__).exception("failed to record an error")


@app.exception_handler(Exception)
async def _record_unhandled(request: Request, exc: Exception):
    """Every unhandled server error files itself in the bug log.

    Until now a 500 existed only in a log nobody was reading, so a fault was
    known about exactly as often as someone happened to notice it and say so.
    Recording it here means the log holds what actually broke, with the endpoint,
    the exception and the traceback, whether or not anyone reported it.

    The response the caller gets is unchanged: a plain 500. Recording must not
    change behaviour, and the traceback stays server-side.
    """
    import traceback

    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    where = f"{request.method} {request.url.path}"
    _record_bug(
        request,
        title=f"[server] {type(exc).__name__} on {where}",
        detail=f"{where}\n\n{type(exc).__name__}: {exc}\n\n{tb}",
        # Type + endpoint, not the message: the same fault carrying a different
        # id in its text is still one fault.
        fingerprint=f"server|{type(exc).__name__}|{request.url.path}",
        severity="blocker", status=500, message=f"{type(exc).__name__}: {exc}",
    )
    logging.getLogger(__name__).exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.get("/api/admin/diagnostics")
def diagnostics(_: object = Depends(require_admin_dep), db=Depends(get_db_dep)) -> dict:
    """What the database actually looks like, from inside the running server.

    Every round of "it 500s" has cost a full deploy cycle to guess at, because a
    500 in a browser console carries nothing and the person reading it cannot see
    the server log. This answers the questions those guesses were about: which
    build is running, which database it is talking to, which tables the models
    expect that are not there, and whether each admin feature's own tables are
    usable. Admin only. It reports table NAMES and row counts — no row contents,
    no credentials, and the database URL is never included.
    """
    from sqlalchemy import inspect as _inspect

    from .db import Base, engine

    info: dict = {"version": VERSION, "built_at": BUILT_AT,
                  "dialect": engine.dialect.name}
    try:
        present = set(_inspect(engine).get_table_names())
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info

    expected = set(Base.metadata.tables)
    info["tables_missing"] = sorted(expected - present)
    info["tables_present"] = len(present & expected)

    # Actually touch the tables the admin screens use. A table can exist and
    # still be unusable — a column the models added but the database never got is
    # exactly the shape of failure that reads as a 500 on one screen only.
    checks: dict[str, str] = {}
    from .models import AppSetting, AssistantLog, User
    for label, model in (("users", User), ("assistant_logs", AssistantLog),
                         ("app_settings", AppSetting)):
        try:
            checks[label] = f"ok ({db.query(model).count()} rows)"
        except Exception as exc:
            db.rollback()
            checks[label] = f"{type(exc).__name__}: {str(exc)[:200]}"
    info["checks"] = checks

    # Can this server hash a password at all? Creating a user and setting a
    # password both hash; signing in only VERIFIES, and verification answers
    # "wrong password" on any error. So a broken bcrypt shows up as 500 on those
    # two admin actions and 401 on every login, and nothing anywhere says bcrypt.
    from .security import hashing_selftest
    info["password_hashing"] = hashing_selftest()

    # What the two batches actually call their suburbs.
    #
    # "Pick a suburb and nothing happens" on the properties page while the same
    # picker works on trends has one remaining explanation I cannot test from
    # here: the dropdown is built from the SOLD archive merged with the live
    # listings, and if the two feeds spell or scope suburbs differently, every
    # name the list offers can be a name no live listing carries. Then no
    # selection matches anything, for any suburb — which looks exactly like a
    # dead control rather than an empty result.
    #
    # District has a small shared vocabulary, which is why it keeps working.
    #
    # This prints both vocabularies side by side so the question is answered by
    # looking rather than by another theory. Place names only.
    try:
        from .models import ImportBatch, PropertyForSale, PropertySold

        areas: dict = {}
        for kind, model in (("for_sale", PropertyForSale), ("sold", PropertySold)):
            batch = (db.query(ImportBatch.id)
                     .filter(ImportBatch.batch_type == kind,
                             ImportBatch.is_active.is_(True))
                     .order_by(ImportBatch.id.desc()).first())
            if not batch:
                areas[kind] = {"batch": None}
                continue
            subs = [s for (s,) in db.query(model.suburb)
                    .filter(model.import_batch_id == batch[0])
                    .distinct().all() if s]
            dis = [d for (d,) in db.query(model.district)
                   .filter(model.import_batch_id == batch[0])
                   .distinct().all() if d]
            areas[kind] = {
                "batch": batch[0],
                "distinct_suburbs": len(subs),
                "sample_suburbs": sorted(subs)[:12],
                "distinct_districts": len(dis),
                "sample_districts": sorted(dis)[:12],
            }
        fs = {s.strip().lower() for s in areas.get("for_sale", {}).get("sample_suburbs", [])}
        overlap = None
        try:
            live = {s for (s,) in db.query(PropertyForSale.suburb).distinct().all() if s}
            sold = {s for (s,) in db.query(PropertySold.suburb).distinct().all() if s}
            shared = {s.strip().lower() for s in live} & {s.strip().lower() for s in sold}
            overlap = {"live_only": len({s.strip().lower() for s in live} - shared),
                       "sold_only": len({s.strip().lower() for s in sold} - shared),
                       "in_both": len(shared)}
        except Exception:
            pass
        areas["suburb_overlap"] = overlap
        info["areas"] = areas
    except Exception as exc:
        db.rollback()
        info["areas"] = f"{type(exc).__name__}: {exc}"
    return info


@app.get("/api/version")
def version() -> dict[str, str]:
    """Which build is actually serving. Deliberately unauthenticated.

    The one time you most need to know whether a deploy landed is when something
    is broken — and "something is broken" has more than once meant nobody could
    sign in. A version endpoint behind the login is no use in exactly the case it
    exists for. It exposes nothing: a build number and the date it was cut.
    """
    return {"version": VERSION, "built_at": BUILT_AT}


# A build marker baked into the source. If /api/selftest shows this exact string,
# the server is running THIS code (the pricing fix). If the page 404s or shows a
# different marker, the deploy did not take and the old code is still serving.
BUILD_MARKER = "pricing-fix-cvguard-2026-08-05"


@app.get("/api/selftest")
@app.get("/selftest")
def selftest() -> Response:
    """One-click deploy verifier. Runs the real cv_over_guard on 7 Wynne Gray's
    numbers (CV $1.975M, live-bug value $3.362M): the FIXED code returns ~$1.88M.
    Also reports whether a SOLD batch exists — without one, re-price aborts and
    changes nothing. No auth, returns HTML so it's readable in a browser."""
    # 1) Is the pricing fix actually in the running code? Test BOTH the guard AND
    # the land-only gate — the real bug was the land-only path skipping the guard,
    # so testing the guard alone gave a false PASS. 7 Wynne Gray (CV/floor
    # $8,476/m²) must NOT be land-only; 31 Pekanga ($1,987/m²) must be.
    try:
        from .pricing.pipeline import (CV_OVER_TOLERANCE,
                                       LAND_ONLY_MAX_CV_PER_FLOOR_M2,
                                       cv_over_guard)
        fixed_val, fired = cv_over_guard(3_362_000, 1_975_000, 1_910_000, 0.95)
        wynne_is_land_only = (1_975_000 / 233) < LAND_ONLY_MAX_CV_PER_FLOOR_M2   # must be False
        pekanga_is_land_only = (610_000 / 307) < LAND_ONLY_MAX_CV_PER_FLOOR_M2   # must be True
        guard_present = True
    except Exception as e:  # pragma: no cover
        CV_OVER_TOLERANCE = None
        fixed_val, fired, guard_present = None, False, False
        wynne_is_land_only, pekanga_is_land_only = True, False
        guard_err = f"{type(e).__name__}: {e}"

    guard_ok = (guard_present and fired and fixed_val is not None
                and fixed_val < 2_100_000
                and not wynne_is_land_only and pekanga_is_land_only)

    # 2) Can re-price find a sold batch? Mirror reprice._sold_df EXACTLY: the
    # newest sold batch whose status is staged OR published (NOT is_active — a
    # staged batch is inactive but re-price still uses it, so checking is_active
    # gave a false "must publish" alarm). Report the batch's status + region so
    # a region mismatch (re-price defaults to Auckland) is visible too.
    sold_status = "none"
    sold_region = "-"
    sold_rows = 0
    try:
        from .models import BatchType, ImportBatch, PropertySold
        db = SessionLocal()
        try:
            sold_batch = (
                db.query(ImportBatch)
                .filter(ImportBatch.batch_type == BatchType.SOLD.value,
                        ImportBatch.status.in_(("staged", "published")))
                .order_by(ImportBatch.id.desc())
                .first()
            )
            if sold_batch is not None:
                sold_status = sold_batch.status
                sold_region = sold_batch.region
                sold_rows = (db.query(PropertySold)
                             .filter(PropertySold.import_batch_id == sold_batch.id)
                             .count())
        finally:
            db.close()
    except Exception as e:  # pragma: no cover
        sold_err = f"{type(e).__name__}: {e}"

    reprice_can_run = sold_status in ("staged", "published") and sold_rows > 0

    def badge(ok: bool) -> str:
        return ('<span style="color:#0A8754;font-weight:700">PASS</span>' if ok
                else '<span style="color:#D4503E;font-weight:700">FAIL</span>')

    html = f"""<!doctype html><meta charset=utf-8>
<title>Apex Property backend self-test</title>
<body style="font-family:system-ui,Arial;max-width:720px;margin:40px auto;line-height:1.5">
<h2>Apex Property backend self-test</h2>
<p>Build marker: <code>{BUILD_MARKER}</code></p>
<hr>
<h3>1. Is the pricing fix running? {badge(guard_ok)}</h3>
<p>7 Wynne Gray — CV $1,975,000, old-bug value <b>$3,362,000</b>.<br>
The fixed guard re-values it to:
<b style="font-size:1.3em">{('$'+format(fixed_val, ',.0f')) if fixed_val is not None else 'ERROR'}</b>
(guard fired: {fired}).</p>
<p>Land-only gate — 7 Wynne Gray flagged land-only? <b>{wynne_is_land_only}</b> (must be False)
&nbsp;|&nbsp; 31 Pekanga? <b>{pekanga_is_land_only}</b> (must be True)</p>
<p>{'&#10004; Fixed code is live — guard caps at CV, and the land-only path no longer misfires on normal homes.' if guard_ok
   else '&#10006; FAIL — either the guard did not cap it, or the land-only gate is wrong (a normal home is being flagged land-only and skipping the guard). Redeploy the fixed backend.'}</p>
<hr>
<h3>2. Can re-price run? {badge(reprice_can_run)}</h3>
<p>Newest sold batch status: <b>{sold_status}</b> &nbsp;|&nbsp; region: <b>{sold_region}</b>
&nbsp;|&nbsp; rows in it: <b>{sold_rows:,}</b></p>
<p>{'&#10004; A staged/published sold batch exists — re-price will price against it (no publish needed).' if reprice_can_run
   else '&#10006; No staged or published sold batch found. Re-price will abort with &quot;no sold batch to price against&quot;. Upload a SOLD CSV (it lands staged and is usable immediately).'}</p>
<hr>
<p style="color:#666;font-size:.9em">Overall: {badge(guard_ok and reprice_can_run)} —
{'ready: fixed code + sold data present.' if (guard_ok and reprice_can_run)
 else 'not ready. Fix whichever check shows FAIL above.'}</p>
</body>"""
    return Response(content=html, media_type="text/html")


@app.get("/")
def root() -> dict[str, str]:
    return {"name": "Apex Property API", "docs": "/docs"}
