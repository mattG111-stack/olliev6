from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from .config import settings
from .db import SessionLocal
from .routers import admin_metrics, admin_upload, assistant, auth, billing, dashboards, geo, properties, release, wishlists
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
    title="Apex Property API",
    version="0.1.0",
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
