from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from .config import settings
from .db import SessionLocal
from .routers import admin_metrics, admin_upload, assistant, auth, billing, dashboards, properties, release, wishlists
from .security import ensure_seed_admin, require_active


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    title="Ollie Property Intelligence API",
    version="0.1.0",
    description="Backend for the Ollie property platform.",
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
app.include_router(properties.sold_router, dependencies=PAYWALL)
app.include_router(dashboards.router, dependencies=PAYWALL)
app.include_router(admin_upload.router)
app.include_router(admin_metrics.router)
app.include_router(release.router)
app.include_router(billing.router)
app.include_router(wishlists.router, dependencies=PAYWALL)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
    # 1) Is the pricing fix actually in the running code?
    try:
        from .pricing.pipeline import CV_OVER_TOLERANCE, cv_over_guard
        fixed_val, fired = cv_over_guard(3_362_000, 1_975_000, 1_910_000, 0.95)
        guard_present = True
    except Exception as e:  # pragma: no cover
        CV_OVER_TOLERANCE = None
        fixed_val, fired, guard_present = None, False, False
        guard_err = f"{type(e).__name__}: {e}"

    guard_ok = guard_present and fired and fixed_val is not None and fixed_val < 2_100_000

    # 2) Does a SOLD comps batch exist? Re-price aborts without one.
    sold_active = 0
    sold_rows = 0
    try:
        from .models import BatchType, ImportBatch, PropertySold
        db = SessionLocal()
        try:
            sold_active = (
                db.query(ImportBatch)
                .filter(ImportBatch.batch_type == BatchType.SOLD.value,
                        ImportBatch.is_active.is_(True))
                .count()
            )
            sold_rows = db.query(PropertySold).count()
        finally:
            db.close()
    except Exception as e:  # pragma: no cover
        sold_err = f"{type(e).__name__}: {e}"

    reprice_can_run = sold_active > 0 and sold_rows > 0

    def badge(ok: bool) -> str:
        return ('<span style="color:#0A8754;font-weight:700">PASS</span>' if ok
                else '<span style="color:#D4503E;font-weight:700">FAIL</span>')

    html = f"""<!doctype html><meta charset=utf-8>
<title>Ollie backend self-test</title>
<body style="font-family:system-ui,Arial;max-width:720px;margin:40px auto;line-height:1.5">
<h2>Ollie backend self-test</h2>
<p>Build marker: <code>{BUILD_MARKER}</code></p>
<hr>
<h3>1. Is the pricing fix running? {badge(guard_ok)}</h3>
<p>7 Wynne Gray — CV $1,975,000, old-bug value <b>$3,362,000</b>.<br>
The fixed guard re-values it to:
<b style="font-size:1.3em">{('$'+format(fixed_val, ',.0f')) if fixed_val is not None else 'ERROR'}</b>
(guard fired: {fired}).</p>
<p>{'&#10004; Fixed code is live — the guard caps it below $2.1M.' if guard_ok
   else '&#10006; The guard did NOT cap it. This server is running OLD code — redeploy the fixed backend.'}</p>
<hr>
<h3>2. Can re-price run? {badge(reprice_can_run)}</h3>
<p>Active SOLD batch: <b>{sold_active}</b> &nbsp;|&nbsp; total sold rows: <b>{sold_rows:,}</b></p>
<p>{'&#10004; A sold batch exists — re-price will re-value listings.' if reprice_can_run
   else '&#10006; No active sold batch. Re-price will abort with &quot;no sold batch to price against&quot; and change nothing. Upload a SOLD CSV first.'}</p>
<hr>
<p style="color:#666;font-size:.9em">Overall: {badge(guard_ok and reprice_can_run)} —
{'ready: fixed code + sold data present.' if (guard_ok and reprice_can_run)
 else 'not ready. Fix whichever check shows FAIL above.'}</p>
</body>"""
    return Response(content=html, media_type="text/html")


@app.get("/")
def root() -> dict[str, str]:
    return {"name": "Ollie API", "docs": "/docs"}
