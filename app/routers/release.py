"""Admin endpoints for the two-stage weekly publish.

Flow: upload (stages the data) → GET /staged (review the flags) → fix any held
rows (PATCH) → POST /publish (goes live). Held rows can be published individually
once fixed (POST /listings/{id}/publish).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_db
from ..models import (
    BatchType,
    ImportBatch,
    IngestJob,
    PropertyForSale,
    PropertyRent,
    PropertySold,
    User,
)
from ..release import publish_release, staged_summary
from ..runlog import record as _record
from ..security import require_admin
log = logging.getLogger(__name__)

from ..staged_stages import (
    _staged_forsale_batch,
    abandon_stage,
    enrichable_forsale_batch,
    create_stage_job,
    run_enrich_job,
    run_price_job,
    stage_running,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---- review the staged release ----------------------------------------------
class StagedOut(BaseModel):
    has_staged: bool
    sold_batch_id: int | None
    forsale_batch_id: int | None
    sold_rows: int
    # Every sold row in the system, not just this upload's — see StagedSummary.
    sold_total: int = 0
    forsale_rows: int
    forsale_rejected: int
    held_total: int
    hold_reasons: dict[str, int]
    pv_checked: int
    pv_wanted: int = 0
    pv_pending: int
    uploaded_at: str | None
    # "staged" (still being worked on) or "preview" (finished, being checked
    # while the site still shows the previous load).
    stage: str | None = None


@router.get("/release/staged", response_model=StagedOut)
def get_staged(region: str = "Auckland", _: User = Depends(require_admin),
               db: Session = Depends(get_db)) -> StagedOut:
    return StagedOut(**staged_summary(db, region).__dict__)


# ---- staged review grid ------------------------------------------------------
# The row grid beneath the filter chips: every figure needed to inspect a batch
# before publish. The four profit figures are DISTINCT columns and must not be
# collapsed (see StagedGridRow docstring).
class StagedGridRow(BaseModel):
    """One staged listing, with the four distinct deal figures spelled out:

      valuation             — what it's worth as-is (fair_value, anchor-guarded).
      margin_dollars / _pct — fair_value − asking: a straight underpriced-house buy.
      subdivision_profit     — what you clear after developing the lots ($).
      subdivision_profit_pct — that profit as a return on total development cost.

    valuation is NOT market_value (that is asking × 0.95): it is the anchor-guarded
    fair_value, so a broken CV can't surface a fake number. `buy_price` (what you
    can pay) is a separate figure from `valuation` (what it's worth) — two labels,
    never one, so the grid never reads as an instruction to overpay.
    """
    id: int
    address: str | None
    suburb: str | None
    property_type: str | None
    asking_price: float | None
    cv_numeric: float | None
    valuation: float | None            # fair_value (anchor-guarded)
    buy_price: float | None            # what you can pay (≤ asking); distinct from valuation
    vs_cv_pct: float | None            # valuation / CV − 1 (the data-quality sort)
    margin_dollars: float | None       # valuation − asking
    margin_pct: float | None           # margin
    # WHY the margin is blank, in words, when it is blank.
    #
    # A blank margin column and a blank "we could not price this" column look
    # identical in a spreadsheet, and they are opposite problems. On the last
    # load 317 listings were valued at least 5% above their asking price and had
    # the margin withheld anyway — $56.7M of it — and the export gave no way to
    # tell which rule did it, so the only way to find out was to read the
    # pricing code and guess at which branch each row took.
    deal_block_reason: str | None = None
    # Where the asking price came from.
    #
    # A house that had a price last week and now says by-negotiation gets last
    # week's price carried forward, less a negotiation discount, rounded. That
    # has been happening and there was no way to see it: the figure appeared in
    # the Asking column looking exactly like an advertised price, and the export
    # had no column that said otherwise. So "which of these prices did a vendor
    # actually name" was unanswerable, and so was "did the carry-forward run".
    asking_basis: str | None = None        # advertised | last advertised, less 3%
    prior_asking_price: float | None = None   # what it was on the market at
    prior_asking_seen_at: str | None = None   # and when we last saw that price
    subdivision_profit: float | None   # $ cleared after developing
    subdivision_profit_pct: float | None  # return on total development cost
    gross_realisation: float | None    # gross_sales — the lots' sale total
    development_cost: float | None      # services_cost — the inputs behind the profit
    lots: float | None                 # max_addl_lots
    buy_score: float | None            # opportunity_score_pct
    last_sold_price: float | None      # CoreLogic's last sale, else the scraper's
    last_sold_date: str | None
    floor_area_m2: float | None
    land_area_m2: float | None
    comps_used: int | None
    confidence: str | None
    is_subdividable: bool | None
    best_strategy: str | None
    is_held: bool | None
    hold_reason: str | None
    pv_checked: bool


class StagedGrid(BaseModel):
    batch_id: int | None
    total: int              # rows in the staged batch
    filtered: int           # rows matching the active chip
    counts: dict[str, int]  # per-chip counts, for the chip labels
    rows: list[StagedGridRow]


_GRID_FILTERS = ("all", "held", "unpriced", "not_enriched", "corelogic_missed")


def _needs_enrich(p: PropertyForSale) -> bool:
    """A pricing-critical field (floor / land / CV) is still blank."""
    return (p.floor_area_m2 is None or p.floor_area_m2 == 0
            or p.land_area_m2 is None or p.land_area_m2 == 0
            or p.cv_numeric is None or p.cv_numeric == 0)


def _grid_row(p: PropertyForSale) -> StagedGridRow:
    val = p.fair_value
    asking = p.asking_price
    # Margin $ only means something when the pricing engine endorsed a margin.
    # It withholds the margin (p.margin is None) when the asking isn't a real
    # price — a by-negotiation placeholder (asking == CV to the dollar), a guide/
    # "offers over" lure, a stale listing, or a premium home. Showing val − asking
    # anyway prints an absurd figure (6 Cassino Terrace: a $2.16M "margin" off a
    # $500k placeholder asking). Tie the dollar figure to the endorsed margin so
    # the two columns can't disagree.
    margin_dollars = ((val - asking)
                      if (val is not None and asking is not None and p.margin is not None)
                      else None)
    vs_cv = ((val / p.cv_numeric) - 1) if (val and p.cv_numeric) else None
    # Subdivision profit % = profit as a return on TOTAL development cost. Total
    # cost = everything spent = gross realisation − profit, so the ratio needs no
    # extra inputs. Guards against a non-positive cost base.
    sp, gr = p.subdivision_profit, p.gross_sales
    sub_pct = None
    if sp is not None and gr is not None:
        cost = gr - sp
        if cost > 0:
            sub_pct = sp / cost
    return StagedGridRow(
        id=p.id, address=p.address, suburb=p.suburb, property_type=p.property_type,
        asking_price=asking, cv_numeric=p.cv_numeric,
        valuation=val, buy_price=p.buy_price,
        vs_cv_pct=vs_cv, margin_dollars=margin_dollars, margin_pct=p.margin,
        subdivision_profit=sp, subdivision_profit_pct=sub_pct,
        gross_realisation=gr, development_cost=p.services_cost,
        lots=p.max_addl_lots, buy_score=p.opportunity_score_pct,
        # Last sold: CoreLogic's record (captured during enrich) when we have it,
        # otherwise the scraper's own last-sold from the CSV.
        last_sold_price=(p.pv_last_sale_price if p.pv_last_sale_price is not None
                         else p.valuation_last_sold_value),
        last_sold_date=(p.pv_last_sale_date or p.valuation_last_sold_date),
        floor_area_m2=p.floor_area_m2, land_area_m2=p.land_area_m2,
        comps_used=p.comps_used, confidence=p.confidence,
        deal_block_reason=p.deal_block_reason,
        asking_basis=p.asking_basis,
        prior_asking_price=p.prior_asking_price,
        prior_asking_seen_at=(p.prior_asking_seen_at.isoformat()
                              if p.prior_asking_seen_at else None),
        is_subdividable=p.is_subdividable, best_strategy=p.best_strategy,
        is_held=p.is_held, hold_reason=p.hold_reason,
        pv_checked=p.pv_checked_at is not None,
    )


@router.get("/release/rows", response_model=StagedGrid)
def staged_rows(region: str = "Auckland", filter: str = "all",
                limit: int = 20000, _: User = Depends(require_admin),
                db: Session = Depends(get_db)) -> StagedGrid:
    """Every staged for-sale row for the review grid, filtered by the active chip.

    The frontend sorts these client-side (four distinct profit columns, default
    margin descending) and exports the filtered+sorted set to CSV — so a batch can
    be inspected, and checked in Excel, before it ever goes live."""
    if filter not in _GRID_FILTERS:
        raise HTTPException(status_code=400, detail=f"filter must be one of {_GRID_FILTERS}")
    batch = _staged_forsale_batch(db, region)
    if batch is None:
        return StagedGrid(batch_id=None, total=0, filtered=0,
                          counts={k: 0 for k in _GRID_FILTERS}, rows=[])
    recs = (db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch.id)
            .order_by(PropertyForSale.id).all())

    def _match(p: PropertyForSale, f: str) -> bool:
        if f == "all":
            return True
        if f == "held":
            return bool(p.is_held)
        if f == "unpriced":
            return p.fair_value is None
        if f == "not_enriched":
            return p.pv_checked_at is None and _needs_enrich(p)
        if f == "corelogic_missed":
            return p.pv_checked_at is not None and _needs_enrich(p)
        return True

    counts = {f: sum(1 for p in recs if _match(p, f)) for f in _GRID_FILTERS}
    selected = [p for p in recs if _match(p, filter)][:limit]
    return StagedGrid(
        batch_id=batch.id, total=len(recs), filtered=counts[filter],
        counts=counts, rows=[_grid_row(p) for p in selected],
    )


class HeldRow(BaseModel):
    id: int
    address: str | None
    suburb: str | None
    property_type: str | None
    hold_reason: str | None
    beds: int | None
    baths: int | None
    floor_area_m2: float | None
    land_area_m2: float | None
    cv_numeric: float | None
    zoning: str | None
    asking_price: float | None
    # CoreLogic's values, to fix against
    pv_cv: float | None
    pv_estimate_mid: float | None

    class Config:
        from_attributes = True


@router.get("/release/held", response_model=list[HeldRow])
def list_held(region: str = "Auckland", batch_id: int | None = None,
              _: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[PropertyForSale]:
    """The DATA-QUALITY held rows in the staged (or a given) batch — the fix-&-publish
    queue. Below-margin exclusions (no data problem, just no deal) are deliberately
    left out so they don't swamp this list; browse those via the review grid's
    'held' filter, which paginates and carries the per-row enrich / re-price actions."""
    from ..release import BELOW_MARGIN_REASON, NO_ASKING_REASON
    q = (db.query(PropertyForSale)
         .filter(PropertyForSale.is_held.is_(True),
                 PropertyForSale.hold_reason.notin_((BELOW_MARGIN_REASON, NO_ASKING_REASON))))
    if batch_id:
        q = q.filter(PropertyForSale.import_batch_id == batch_id)
    return q.order_by(PropertyForSale.hold_reason, PropertyForSale.id).limit(1000).all()


# ---- where did the deals go? ------------------------------------------------
class FunnelStep(BaseModel):
    label: str
    kept: int
    lost: int
    why: str


class DealFunnelOut(BaseModel):
    batch_id: int | None
    total: int
    steps: list[FunnelStep]
    hold_reasons: list[tuple[str, int]]
    flagged: int
    # Rows that pass every condition of the deal rule and are STILL not flagged.
    # Always expect 0 — see app/deal_funnel.py for why anything else is a
    # write-ordering problem and not a pricing problem.
    mismatch: int
    mismatch_examples: list[str]
    orphan_flags: int


class RunEventOut(BaseModel):
    at: datetime
    stage: str
    level: str
    event: str
    detail: str | None
    count: int | None
    address: str | None


def _batch_or_live(db: Session, region: str, batch_id: int | None) -> int | None:
    """Default every diagnostic to the LIVE batch. The question behind all of
    them is "what are customers being shown", and answering it about a staged
    batch nobody has published is a different question that looks identical."""
    if batch_id is not None:
        return batch_id
    b = (db.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE,
                 ImportBatch.region == region,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    return b.id if b else None


@router.get("/release/run-log", response_model=list[RunEventOut])
def get_run_log(region: str = "Auckland", batch_id: int | None = None,
                _: User = Depends(require_admin),
                db: Session = Depends(get_db)) -> list[RunEventOut]:
    """Everything the four stages decided about one load, oldest first.

    In order, because the order is the explanation: a rejection at load is why a
    suburb is thin at pricing, and a blocked lookup is why a row has no floor
    area to be valued on. Read as a list of totals it says nothing.
    """
    from ..runlog import events

    return [RunEventOut(at=e.at, stage=e.stage, level=e.level, event=e.event,
                        detail=e.detail, count=e.count, address=e.address)
            for e in events(db, _batch_or_live(db, region, batch_id))]


@router.get("/release/run-log.xlsx")
def get_run_log_xlsx(region: str = "Auckland", batch_id: int | None = None,
                     _: User = Depends(require_admin),
                     db: Session = Depends(get_db)):
    """The same thing as a workbook: the log, the funnel, every listing with the
    decisions attached, the deals, and the held rows."""
    from fastapi.responses import Response

    from ..runlog_export import build

    bid = _batch_or_live(db, region, batch_id)
    if bid is None:
        raise HTTPException(404, "No load to export yet.")
    b = db.get(ImportBatch, bid)
    stamp = (b.created_at.strftime("%d-%m-%Y") if (b and b.created_at) else "load")
    return Response(
        content=build(db, bid),
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={"Content-Disposition":
                 f'attachment; filename="apex-load-{bid}-{stamp}.xlsx"'},
    )


@router.get("/release/deal-funnel", response_model=DealFunnelOut)
def get_deal_funnel(region: str = "Auckland", batch_id: int | None = None,
                    _: User = Depends(require_admin),
                    db: Session = Depends(get_db)) -> DealFunnelOut:
    """Count the batch after every gate between "a listing" and "a deal".

    Defaults to the LIVE batch, because the question this answers is always
    about what customers are being shown. Pass batch_id to point it at a staged
    batch instead.
    """
    from ..deal_funnel import deal_funnel

    f = deal_funnel(db, _batch_or_live(db, region, batch_id))
    return DealFunnelOut(
        batch_id=f.batch_id, total=f.total,
        steps=[FunnelStep(label=s.label, kept=s.kept, lost=s.lost, why=s.why)
               for s in f.steps],
        hold_reasons=f.hold_reasons, flagged=f.flagged,
        mismatch=f.mismatch, mismatch_examples=f.mismatch_examples,
        orphan_flags=f.orphan_flags)


# ---- operator-triggered stages: ENRICH + PRICE ------------------------------
# Each stage runs on a background thread with its own DB session, so the request
# returns at once and /health keeps answering while the (long) stage runs. Poll
# GET /api/admin/jobs/{job_id} for durable progress. Both are re-runnable.
class StageStarted(BaseModel):
    job_id: int
    batch_id: int
    stage: str


@router.post("/release/enrich", response_model=StageStarted)
def enrich_staged(region: str = "Auckland", admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)) -> StageStarted:
    """Fill blank floor / land / CV from CoreLogic — staged batch, or the live one.

    Re-runnable: a re-run only looks up rows that are STILL missing a
    pricing-critical field, so a stage that died at 60% resumes from there.

    It used to accept ONLY a staged batch, and publishing flips a batch out of
    that status — so the moment a load went live it became impossible to
    bulk-enrich, and the only option left was the per-listing button on a batch
    of eleven thousand. A published batch with thousands of rows still missing a
    floor area is exactly the one that needs this most: those rows are held,
    unpriced, and showing as gaps to customers.
    """
    batch = enrichable_forsale_batch(db, region)
    if batch is None:
        raise HTTPException(
            status_code=409,
            detail=f"No for-sale batch to enrich for {region} — nothing staged "
                   f"and nothing live.")
    # One enrich at a time per batch: a second heavy worker stacked on the first is
    # the main way this OOM-killed the container. Return the in-flight job instead.
    if stage_running(db, batch.id, "enrich"):
        raise HTTPException(status_code=409, detail="Enrich is already running for this batch")
    job = create_stage_job(db, stage="enrich", batch_id=batch.id, region=region,
                           uploaded_by_id=admin.id)
    bid, jid = batch.id, job.id
    threading.Thread(target=run_enrich_job, args=(jid, bid, region), daemon=True).start()
    return StageStarted(job_id=jid, batch_id=bid, stage="enrich")


class LookupCheck(BaseModel):
    ok: bool
    status: str           # ok / not_found / blocked / error
    headline: str         # what to tell the operator, in one line
    detail: str           # what to do about it
    address: str          # what was asked, so the answer can be judged


@router.get("/release/lookup-check", response_model=LookupCheck)
def lookup_check(address: str = "12 Queen Street, Auckland",
                 _: User = Depends(require_admin)) -> LookupCheck:
    """Ask the data provider about one property, and say plainly what came back.

    "The lookup isn't working" has had no answer short of running an enrich over
    a whole batch and reading the summary afterwards. That is a twenty-minute
    question with a one-second answer in it, and the three things it could be
    need three different responses:

        blocked      we are being refused — it clears on its own, wait
        error        the request never arrived — network, DNS or proxy, and
                     nothing is wrong with the addresses
        not_found    it arrived, was answered, and they hold no record — so the
                     connection is fine and this address simply is not there

    One real request, on demand, never on a page load.
    """
    from ..propertyvalue import (PV_BLOCKED, PV_ERROR, PV_NOT_FOUND, PV_OK,
                                 pv_lookup_status)
    try:
        rec, status = pv_lookup_status(address)
    except Exception as e:                     # noqa: BLE001
        status, rec = PV_ERROR, None
        log.warning("lookup check failed: %s: %s", type(e).__name__, e)

    if status == PV_OK:
        got = sorted(k for k, v in (rec or {}).items() if v not in (None, ""))
        return LookupCheck(
            ok=True, status=status, address=address,
            headline="Working — the lookup answered with a property record.",
            detail=(f"{len(got)} field(s) came back. Nothing is wrong with the "
                    f"connection; if a run is still filling nothing, the "
                    f"addresses it asked about are the place to look."))
    if status == PV_BLOCKED:
        return LookupCheck(
            ok=False, status=status, address=address,
            headline="We are being refused — not broken, rate-limited.",
            detail=("This clears on its own. An enrich now backs off and picks "
                    "itself up, so start it and leave it; it does not need "
                    "watching."))
    if status == PV_NOT_FOUND:
        return LookupCheck(
            ok=True, status=status, address=address,
            headline="Working — the connection is fine.",
            detail=("The request arrived and was answered; they simply hold no "
                    "record for this address. Try the check with an address you "
                    "know they have if you want to see a full record."))
    return LookupCheck(
        ok=False, status=status, address=address,
        headline="The request never arrived.",
        detail=("Network, DNS or proxy — not the addresses, and not a rate "
                "limit. Nothing gets marked as checked when this happens, so "
                "everything is still there to fetch once it is fixed."))


class StageRestarted(StageStarted):
    stopped: int          # how many runs were taken off the batch to get here


@router.post("/release/{stage}/restart", response_model=StageRestarted)
def restart_stage(stage: str, region: str = "Auckland",
                  admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)) -> StageRestarted:
    """Take a stuck stage off whatever is holding it and start it again.

    The ordinary Enrich button refuses while a run is in flight, which is right
    — two enrichers on one batch is how the container gets OOM-killed. But it
    left no way out of the case where a run is neither finished nor going
    anywhere: the button answered "already running" and kept answering it, and
    the only cure was to wait the lock out.

    This is the way out. It marks whatever is running as stopped, which the
    running thread notices at its next checkpoint and stands down, then starts a
    fresh run. Enrich resumes rather than restarts, so nothing already looked up
    is paid for twice.

    Safe to press when nothing is running — then it is just a start.
    """
    if stage not in ("enrich", "price"):
        raise HTTPException(status_code=404, detail=f"No stage called {stage!r}")
    batch = (enrichable_forsale_batch(db, region) if stage == "enrich"
             else _staged_forsale_batch(db, region))
    if batch is None:
        raise HTTPException(
            status_code=409,
            detail=f"No for-sale batch to {stage} for {region} — nothing staged "
                   f"and nothing live.")
    stopped = abandon_stage(db, batch.id, stage)
    job = create_stage_job(db, stage=stage, batch_id=batch.id, region=region,
                           uploaded_by_id=admin.id)
    bid, jid = batch.id, job.id
    _record(db, stage=stage, event="restarted", batch_id=bid, job_id=jid,
            count=stopped, level="warn",
            detail=(f"{stage} restarted by an operator"
                    + (f"; {stopped} run(s) already in flight were stopped first"
                       if stopped else " (nothing was running)")))
    fn = run_enrich_job if stage == "enrich" else run_price_job
    threading.Thread(target=fn, args=(jid, bid, region), daemon=True).start()
    return StageRestarted(job_id=jid, batch_id=bid, stage=stage, stopped=stopped)


@router.post("/release/price", response_model=StageStarted)
def price_staged(region: str = "Auckland", admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)) -> StageStarted:
    """Stage 3 — re-run the pricing pipeline over the staged batch using its
    current stored attributes (i.e. after enrich). Re-runnable, so a fix to the
    pricing code re-values the batch without a re-upload."""
    batch = _staged_forsale_batch(db, region)
    if batch is None:
        raise HTTPException(status_code=409, detail="No staged for-sale batch to price")
    if stage_running(db, batch.id, "price"):
        raise HTTPException(status_code=409, detail="Re-price is already running for this batch")
    job = create_stage_job(db, stage="price", batch_id=batch.id, region=region,
                           uploaded_by_id=admin.id)
    bid, jid = batch.id, job.id
    threading.Thread(target=run_price_job, args=(jid, bid, region), daemon=True).start()
    return StageStarted(job_id=jid, batch_id=bid, stage="price")


# ---- publish the release ----------------------------------------------------
@router.post("/release/preview")
def send_preview(region: str = "Auckland", admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)) -> dict:
    """Finish the batch and move it to preview. Nothing a customer sees changes.

    The step that did not exist. Publishing was one-way, so anything wrong with
    a batch was wrong in front of customers before anybody could look at it.
    """
    from ..release import send_to_preview

    summary = staged_summary(db, region)
    if not summary.has_staged:
        raise HTTPException(status_code=409, detail="Nothing loaded to preview")
    result = send_to_preview(db, region)
    if not result["count"]:
        raise HTTPException(status_code=409,
                            detail="This batch is already in preview")
    return result


@router.post("/release/publish")
def publish(region: str = "Auckland", admin: User = Depends(require_admin),
            db: Session = Depends(get_db)) -> dict:
    """Go live. Takes the previewed batch (or a staged one, if preview was
    skipped) and puts it in front of customers, archiving the previous load."""
    summary = staged_summary(db, region)
    if not summary.has_staged:
        raise HTTPException(status_code=409, detail="Nothing staged to publish")
    fs_batch_id = summary.forsale_batch_id
    result = publish_release(db, region)
    result["held_back"] = summary.held_total
    # Record the publish result in its OWN json column — never serialised into the
    # short `stage` label, which is varchar(64) and would truncate (the original
    # StringDataRightTruncation bug).
    job = IngestJob(
        batch_type=BatchType.FOR_SALE.value,
        filename=f"publish (batch {fs_batch_id})" if fs_batch_id else "publish",
        status="completed",
        stage="publish",
        progress_pct=100,
        batch_id=fs_batch_id,
        uploaded_by_id=admin.id,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        result_json=json.dumps(result),
    )
    db.add(job)
    db.commit()
    return result


# ---- reset: wipe all batches + listings back to zero -------------------------
class ResetResult(BaseModel):
    for_sale_deleted: int
    sold_deleted: int
    rent_deleted: int
    batches_deleted: int
    jobs_deleted: int


@router.post("/reset-all", response_model=ResetResult)
def reset_all(confirm: str = "", _: User = Depends(require_admin),
              db: Session = Depends(get_db)) -> ResetResult:
    """Delete every batch, listing and ingest job — a clean slate for re-upload.
    Users, billing and settings are untouched. Requires ?confirm=RESET so it can't
    fire by accident. This is the button behind a messy import history: it takes
    the batch numbering and every stat back to zero."""
    if confirm != "RESET":
        raise HTTPException(status_code=400,
                            detail="Pass confirm=RESET to wipe all batches and listings")
    fs = db.query(PropertyForSale).delete(synchronize_session=False)
    so = db.query(PropertySold).delete(synchronize_session=False)
    re = db.query(PropertyRent).delete(synchronize_session=False)
    jo = db.query(IngestJob).delete(synchronize_session=False)
    ba = db.query(ImportBatch).delete(synchronize_session=False)
    db.commit()
    return ResetResult(for_sale_deleted=fs, sold_deleted=so, rent_deleted=re,
                       batches_deleted=ba, jobs_deleted=jo)


# ---- fix + publish individual held rows -------------------------------------
class ListingPatch(BaseModel):
    beds: int | None = None
    baths: int | None = None
    floor_area_m2: float | None = None
    land_area_m2: float | None = None
    cv_numeric: float | None = None
    zoning: str | None = None
    asking_price: float | None = None


@router.patch("/listings/{listing_id}", response_model=HeldRow)
def edit_listing(listing_id: int, body: ListingPatch, _: User = Depends(require_admin),
                 db: Session = Depends(get_db)) -> PropertyForSale:
    """Fix data-quality fields on a listing (typically a held row before publish)."""
    p = db.get(PropertyForSale, listing_id)
    if not p:
        raise HTTPException(status_code=404, detail="Listing not found")
    for field, val in body.model_dump(exclude_unset=True).items():
        setattr(p, field, val)
    db.commit(); db.refresh(p)
    return p


@router.post("/listings/{listing_id}/publish", response_model=HeldRow)
def publish_listing(listing_id: int, _: User = Depends(require_admin),
                    db: Session = Depends(get_db)) -> PropertyForSale:
    """Release a held listing to the live site (clear the hold)."""
    p = db.get(PropertyForSale, listing_id)
    if not p:
        raise HTTPException(status_code=404, detail="Listing not found")
    p.is_held = False
    p.hold_reason = None
    db.commit(); db.refresh(p)
    return p


class RemovedResult(BaseModel):
    id: int
    address: str | None
    removed: bool


@router.delete("/listings/{listing_id}", response_model=RemovedResult)
def remove_listing(listing_id: int, reason: str = "not a real listing",
                   admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)) -> RemovedResult:
    """Take a listing out of the batch entirely.

    Holding and removing are different answers to different questions, and only
    one of them existed. Hold means "real, but not for the site" — it stays in
    the batch, keeps its valuation, counts in the totals, and can be released
    later. There was nothing for "this is not a house", which is a listing that
    should not be in the numbers at all.

    So this deletes the row. Not a flag: a flagged row still sits in the
    averages, the funnel and the export, and the whole point is that it is not
    real.

    It is written to the run log first, with the address, because the row is
    about to stop existing and a deletion nobody can account for later is worse
    than the bad row was. Re-loading the file brings it back — the feed is the
    source of truth and this does not change the feed.
    """
    p = db.get(PropertyForSale, listing_id)
    if not p:
        raise HTTPException(status_code=404, detail="Listing not found")
    address, batch_id = p.address, p.import_batch_id
    _record(db, stage="publish", event="listing_removed", batch_id=batch_id,
            level="warn", address=address, count=1,
            detail=f"{address or 'a listing'} removed by {admin.email} — {reason}")
    db.delete(p)
    db.commit()
    return RemovedResult(id=listing_id, address=address, removed=True)


@router.post("/listings/{listing_id}/hold", response_model=HeldRow)
def hold_listing(listing_id: int, reason: str = "Held by admin",
                 _: User = Depends(require_admin), db: Session = Depends(get_db)) -> PropertyForSale:
    """Manually hold a listing back from the live site."""
    p = db.get(PropertyForSale, listing_id)
    if not p:
        raise HTTPException(status_code=404, detail="Listing not found")
    p.is_held = True
    p.hold_reason = reason
    db.commit(); db.refresh(p)
    return p


# ---- per-listing manual enrich / re-price -----------------------------------
# Excluded listings (below the deal-margin floor, or unpriced) stay in the DB and
# can be worked one at a time: enrich fills their blanks from CoreLogic, then
# re-price re-values them and lifts the hold if they now clear the margin.
class ListingActionResult(BaseModel):
    id: int
    address: str | None
    fair_value: float | None
    asking_price: float | None
    margin_dollars: float | None       # fair_value − asking (the $ deal margin)
    cv_numeric: float | None
    floor_area_m2: float | None
    land_area_m2: float | None
    is_held: bool
    hold_reason: str | None
    pv_status: str | None = None       # enrich only: ok / missed / blocked / error


def _action_result(p: PropertyForSale, pv_status: str | None = None) -> ListingActionResult:
    md = (p.fair_value - p.asking_price) if (p.fair_value is not None
                                             and p.asking_price is not None) else None
    return ListingActionResult(
        id=p.id, address=p.address, fair_value=p.fair_value, asking_price=p.asking_price,
        margin_dollars=md, cv_numeric=p.cv_numeric, floor_area_m2=p.floor_area_m2,
        land_area_m2=p.land_area_m2, is_held=p.is_held, hold_reason=p.hold_reason,
        pv_status=pv_status)


class LotsIn(BaseModel):
    # None means "use the model" — deliberately different from any number, so
    # clearing an override restores the model's own reading instead of freezing
    # whatever it happened to say today.
    lots: float | None = None


class LotsResult(BaseModel):
    id: int
    address: str | None
    lots_override: float | None      # what a person set, or null for the model's
    sections: int | None             # what the site is now taken to split into
    max_addl_lots: float | None
    best_net_gain: float | None
    subdivision_profit: float | None


@router.post("/listings/{listing_id}/lots", response_model=LotsResult)
def set_listing_lots(listing_id: int, body: LotsIn,
                     _: User = Depends(require_admin),
                     db: Session = Depends(get_db)) -> LotsResult:
    """Set how many lots this site takes, and keep it.

    The scenario calculator on the property page answers "what if" and saves
    nothing — useful for working out a number, no use at all for fixing one. A
    listing reviewed in preview and corrected there has to CARRY the correction
    past the publish, or the review changed nothing.

    So this stores the count on the listing and re-prices it immediately, which
    is what makes the new figures visible in the same review rather than after
    the next full pricing run. The pricing run then honours the stored value
    rather than replacing it with the model's own cautious reading.

    Send {"lots": null} to hand the site back to the model.
    """
    from ..reprice import reprice_one

    p = db.get(PropertyForSale, listing_id)
    if p is None:
        raise HTTPException(status_code=404, detail="No such listing")

    if body.lots is None:
        p.lots_override = None
    else:
        n = float(body.lots)
        if n != n or n in (float("inf"), float("-inf")):
            raise HTTPException(status_code=422, detail="That is not a number of lots.")
        if n < 1:
            raise HTTPException(
                status_code=422,
                detail="A site takes at least one lot. Clear the number to use "
                       "the model's own reading.")
        p.lots_override = float(int(n))       # lots are whole things
    db.commit()

    # Re-price this one listing so the review shows the corrected figures now.
    # Same path the weekly file and the portal fills use, so an edited row is
    # valued by exactly the rules every other row is.
    #
    # The SAVE is what matters and it has already happened. Re-pricing needs a
    # sold batch to value against, and there is not always one — on a fresh
    # database, or before the week's sales are loaded. Losing the operator's
    # number because the figures could not be refreshed yet would be the wrong
    # way round: keep the setting, report the old figures, and the next pricing
    # run picks it up.
    try:
        updated = reprice_one(db, listing_id) or db.get(PropertyForSale, listing_id)
    except ValueError:
        db.commit()
        updated = db.get(PropertyForSale, listing_id)
    _record(db, stage="review", event="lots_set", batch_id=updated.import_batch_id,
            count=int(body.lots) if body.lots else 0, level="info",
            detail=(f"{updated.address}: lot count "
                    + (f"set to {int(body.lots)} by hand" if body.lots
                       else "handed back to the model")))
    return LotsResult(
        id=updated.id, address=updated.address,
        lots_override=updated.lots_override, sections=updated.sections,
        max_addl_lots=updated.max_addl_lots, best_net_gain=updated.best_net_gain,
        subdivision_profit=updated.subdivision_profit)


@router.post("/listings/{listing_id}/enrich", response_model=ListingActionResult)
def enrich_listing(listing_id: int, _: User = Depends(require_admin),
                   db: Session = Depends(get_db)) -> ListingActionResult:
    """CoreLogic-enrich one listing on demand (fills blank floor/land/CV, corrects
    a wrong CV). Re-price afterwards to re-value it on the filled numbers."""
    from ..staged_stages import enrich_one
    p, status = enrich_one(db, listing_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    return _action_result(p, pv_status=status)


@router.post("/listings/{listing_id}/reprice", response_model=ListingActionResult)
def reprice_listing(listing_id: int, region: str = "Auckland",
                    _: User = Depends(require_admin),
                    db: Session = Depends(get_db)) -> ListingActionResult:
    """Re-value one listing from its current attributes and re-evaluate its hold —
    if it now clears the $margin floor it drops back into the live feed."""
    from ..reprice import reprice_one
    try:
        p = reprice_one(db, listing_id, region=region)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if p is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    return _action_result(p)


# ---- optional stage: ask the portals -----------------------------------------
@router.post("/release/portals", response_model=StageStarted)
def portals_staged(region: str = "Auckland", cap: int = 200,
                   admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)) -> StageStarted:
    """Ask Trade Me, OneRoof, realestate.co.nz, homes.co.nz and CoreLogic about
    this release's keepers: fill any field we are missing, and record what each
    of them thinks the property is worth.

    Runs AFTER pricing, over the deal candidates only — a weekly file is
    thousands of listings and a few dozen survive the margin floor, so the
    lookups are spent on those. Held rows keep their own per-listing button.

    Re-runnable, and cheap to press twice: a property asked about in the last
    fortnight is skipped, so a run that died part-way resumes.

    Nothing a portal says is allowed to change a price. Facts fill a blank field
    and never overwrite one we hold; each portal's estimate is stored in that
    portal's own columns and shown as theirs. See app/portals/fill.py.
    """
    from ..portals.runner import run_portal_job

    batch = _staged_forsale_batch(db, region)
    if batch is None:
        raise HTTPException(status_code=409, detail="No staged for-sale batch")
    if stage_running(db, batch.id, "portals"):
        raise HTTPException(status_code=409,
                            detail="The portals are already being asked for this batch")
    job = create_stage_job(db, stage="portals", batch_id=batch.id, region=region,
                           uploaded_by_id=admin.id)
    bid, jid = batch.id, job.id
    threading.Thread(target=run_portal_job, args=(jid, bid, region),
                     kwargs={"cap": cap}, daemon=True).start()
    return StageStarted(job_id=jid, batch_id=bid, stage="portals")


class PortalStatus(BaseModel):
    """Which sources can answer right now, so the button can say so."""
    sources: list[str]
    needs_browser: list[str]
    browser_ready: bool


@router.get("/release/portals/status", response_model=PortalStatus)
def portals_status(_: User = Depends(require_admin)) -> PortalStatus:
    """Trade Me, OneRoof and realestate.co.nz render their figures in the
    browser, so they are reached through Apify and need a token. Without one the
    button still works — it just asks the two that can be read directly."""
    from ..portals.apify import configured

    ready = configured()
    direct = ["corelogic", "homes"]
    browser = ["oneroof", "trademe", "realestate"]
    return PortalStatus(
        sources=direct + (browser if ready else []),
        needs_browser=browser,
        browser_ready=ready,
    )


# ---- what the portals found, waiting to be checked --------------------------
class FindingOut(BaseModel):
    """One thing a portal said, and what we currently hold."""
    id: int
    property_id: int
    address: str | None = None
    suburb: str | None = None
    source: str
    field: str
    kind: str                       # "fact" | "detail" | "estimate"
    value: float | str | None = None
    current: float | str | None = None
    created_at: str | None = None


class FindingsOut(BaseModel):
    pending: int
    findings: list[FindingOut]


@router.get("/release/portals/findings", response_model=FindingsOut)
def portal_findings(status: str = "pending", limit: int = 500,
                    _: User = Depends(require_admin),
                    db: Session = Depends(get_db)) -> FindingsOut:
    """What the portals offered, before any of it is written.

    Nothing here has changed a listing. A figure scraped off someone else's page
    is a claim, and the person who has to defend a valuation should see the
    number before it moves one.
    """
    from ..models import PortalFinding

    q = (db.query(PortalFinding).filter(PortalFinding.status == status)
         .order_by(PortalFinding.property_id, PortalFinding.field))
    rows = q.limit(max(1, min(limit, 2000))).all()
    total = db.query(PortalFinding).filter(PortalFinding.status == "pending").count()

    prop_ids = {r.property_id for r in rows}
    props = {p.id: p for p in db.query(PropertyForSale)
             .filter(PropertyForSale.id.in_(prop_ids)).all()} if prop_ids else {}

    out = []
    for r in rows:
        p = props.get(r.property_id)
        out.append(FindingOut(
            id=r.id, property_id=r.property_id,
            address=getattr(p, "address", None), suburb=getattr(p, "suburb", None),
            source=r.source, field=r.field, kind=r.kind,
            value=r.value_num if r.value_num is not None else r.value_text,
            current=r.current_num if r.current_num is not None else r.current_text,
            created_at=r.created_at.isoformat() if r.created_at else None,
        ))
    return FindingsOut(pending=total, findings=out)


class DecideIn(BaseModel):
    ids: list[int] | None = None    # omit to decide on every pending finding
    approve: bool = True


class DecideOut(BaseModel):
    applied: int
    rejected: int
    skipped: int
    reasons: list[str]


@router.post("/release/portals/decide", response_model=DecideOut)
def decide_findings(body: DecideIn, admin: User = Depends(require_admin),
                    db: Session = Depends(get_db)) -> DecideOut:
    """Approve or reject findings. Approving writes the value and re-prices the
    listing through the same margin floor and hold rules as any other change."""
    from ..models import PortalFinding
    from ..portals.findings import approve, reject

    ids = body.ids
    if not ids:
        ids = [r.id for r in db.query(PortalFinding.id)
               .filter(PortalFinding.status == "pending").all()]

    applied = rejected = skipped = 0
    reasons: list[str] = []
    for fid in ids:
        if body.approve:
            ok, why = approve(db, fid, user_id=admin.id)
            if ok:
                applied += 1
            else:
                skipped += 1
                if why not in reasons and len(reasons) < 10:
                    reasons.append(why)
        else:
            rejected += 1 if reject(db, fid, user_id=admin.id) else 0
    return DecideOut(applied=applied, rejected=rejected, skipped=skipped,
                     reasons=reasons)


# ---- new listings the portals found before the weekly file did ---------------
class NewListingOut(BaseModel):
    id: int
    source: str
    url: str | None
    address: str | None
    suburb: str | None
    property_type: str | None
    price_numeric: float | None
    price_display: str | None
    cv_numeric: float | None
    floor_area_m2: float | None
    land_area_m2: float | None
    beds: float | None
    baths: float | None
    # Scraped and stored all along, and never sent — "where is the rest of the
    # info for these houses" and some of it was already on the row.
    carspaces: float | None = None
    # That portal's OWN valuation. Not an input to ours and never will be — but
    # it is the fastest sanity check there is before agreeing to publish a
    # listing, and it was captured on the row and never sent.
    estimate: float | None = None
    listed_date: str | None
    image_url: str | None
    # Stated on every row, because it is the difference between "this property
    # is not subdividable" and "we have not been told its zone yet".
    has_council_data: bool
    # Sold rows only: set when the sale price sits far from what this suburb has
    # been doing. A warning to read, never a rejection — an exceptional sale is
    # often real, and throwing those away teaches the model a market that does
    # not exist.
    price_flag: str | None = None


class NewListingsOut(BaseModel):
    pending: int
    listings: list[NewListingOut]


@router.get("/release/listings/new", response_model=NewListingsOut)
def new_listings(limit: int = 200, admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)) -> NewListingsOut:
    """Listings the daily sweep found that the weekly file has not reached.

    Pending only. Nothing here is live, and nothing becomes live without
    somebody approving it — a listing scraped off someone else's page is a
    claim, and once it is a row in the live batch it looks exactly like data we
    stand behind.
    """
    from ..models import PortalListing
    from ..portals.listings import pending as pending_listings

    rows = pending_listings(db, limit=limit)
    total = (db.query(PortalListing)
             .filter(PortalListing.status == "pending").count())
    return NewListingsOut(
        pending=total,
        listings=[
            NewListingOut(
                id=r.id, source=r.source, url=r.url, address=r.address,
                suburb=r.suburb, property_type=r.property_type,
                price_numeric=r.price_numeric, price_display=r.price_display,
                cv_numeric=r.cv_numeric, floor_area_m2=r.floor_area_m2,
                land_area_m2=r.land_area_m2, beds=r.beds, baths=r.baths,
                carspaces=r.carspaces, estimate=r.estimate,
                listed_date=r.listed_date, image_url=r.image_url,
                has_council_data=bool(r.cv_numeric),
            )
            for r in rows
        ],
    )


def _run_sweep_job(job_id: int, *, kind: str, hours: int, cap: int) -> None:
    """One sweep, on its own thread, writing progress to the job row.

    Never raises — a background thread that dies takes its reason with it, and
    the job row is the only place anyone can see what happened.
    """
    from ..portals.listings import sweep, sweep_sold
    from ..staged_stages import _update

    db = SessionLocal()
    try:
        _update(db, job_id, status="running", stage="listings",
                started_at=datetime.now(timezone.utc), progress_pct=5)
        got = (sweep_sold(db, cap=cap) if kind == "sold"
               else sweep(db, hours=hours, cap=cap))
        new = sum(v.get("new", 0) for v in got.values())
        found = sum(v.get("found", 0) for v in got.values())
        # `stage` is a plain string column and it is what the job card shows,
        # so the summary goes there. IngestJob has no free-text note field, and
        # _update passes its kwargs straight to .update() — an unknown key is a
        # crash inside a background thread, which is the worst place for one.
        summary = "; ".join(f"{k} {v.get('found', 0)} found/{v.get('new', 0)} new"
                            for k, v in got.items()) or "nothing returned"
        _update(db, job_id, status="completed", progress_pct=100,
                completed_at=datetime.now(timezone.utc),
                stage=summary[:60],
                rows_total=found, rows_inserted=new, error_message=None)
    except Exception as e:                        # noqa: BLE001
        log.exception("listings sweep failed")
        try:
            _update(db, job_id, status="failed", stage="done",
                    completed_at=datetime.now(timezone.utc),
                    error_message=f"{type(e).__name__}: {e}"[:480])
        except Exception:                         # noqa: BLE001
            pass
    finally:
        db.close()


@router.post("/release/listings/sweep", response_model=StageStarted)
def sweep_new_listings(hours: int = 24, cap: int = 300,
                       admin: User = Depends(require_admin),
                       db: Session = Depends(get_db)) -> StageStarted:
    """Ask the portals what went on the market recently.

    Returns a JOB, immediately. It does not do the work in the request.
    
    An Apify actor takes tens of seconds to a few minutes, and this asks two of
    them — up to six minutes with the default timeouts. Held open, that request
    is cut off by the platform's proxy long before the answer exists, and what
    the browser gets is a 500 with no body: the sweep may have worked perfectly
    and nobody will ever know. The portals button has always been a background
    job for exactly this reason; this one was written synchronously and hit it
    on the first press.

    `hours` is there for catching up after an outage: a missed day is a day of
    listings nobody saw.
    """
    from ..portals.apify import token

    if not token(db):
        raise HTTPException(
            status_code=400,
            detail="No Apify token yet — add one in the Data connection panel.")
    # No batch: a sweep is looking for properties that are not in one yet. The
    # column is a NULLABLE foreign key, so None is the only correct value — 0
    # would violate the constraint on Postgres and pass on SQLite, which is the
    # worst possible combination.
    job = create_stage_job(db, stage="listings", batch_id=None,
                           region="Auckland", uploaded_by_id=admin.id)
    jid = job.id
    threading.Thread(target=_run_sweep_job, args=(jid,),
                     kwargs={"kind": "for_sale", "hours": hours, "cap": cap},
                     daemon=True).start()
    return StageStarted(job_id=jid, batch_id=0, stage="listings")


class FillOut(BaseModel):
    looked_up: int
    fields_filled: int
    # The one that decides whether these rows can be priced at all.
    council_records_found: int
    not_found: int
    blocked: int
    unreachable: int


@router.post("/release/listings/fill", response_model=FillOut)
def fill_new_listings(kind: str = "for_sale",
                      admin: User = Depends(require_admin),
                      db: Session = Depends(get_db)) -> FillOut:
    """Fill in what the portal didn't carry, BEFORE anyone approves it.

    A portal advertises a listing; it does not publish a council record. So a
    scraped row arrives with an address, a price and a floor area and no CV —
    and a listing with no CV cannot be valued, because every method here is
    anchored on it.

    The only chance to fill that gap used to come after approval, once the row
    was already live. So the decision was made on the thinnest version of the
    row, and a listing marked "no council record" was approved into a hold.

    Only fills blanks, never touches the asking price, and approves nothing.
    """
    from ..portals.complete import fill_pending
    from ..runlog import record

    out = fill_pending(db, kind=kind)
    record(db, stage="portals", event="pending_filled", count=out["looked_up"],
           level="warn" if out["unreachable"] else "info",
           detail=(f"{out['looked_up']:,} new listings looked up — "
                   f"{out['fields_filled']:,} blank fields filled, "
                   f"{out['council_records_found']:,} council records found, "
                   f"{out['not_found']:,} not on the council record, "
                   f"{out['unreachable']:,} never reached it"))
    return FillOut(**out)


@router.post("/release/listings/decide", response_model=DecideOut)
def decide_new_listings(body: DecideIn, admin: User = Depends(require_admin),
                        db: Session = Depends(get_db)) -> DecideOut:
    """Approve or reject new listings. Approving adds the property to the live
    batch and prices it through the same rules — and the same hold rules — as
    the weekly file, so one that cannot be valued honestly is held rather than
    shown."""
    from ..models import PortalListing
    from ..portals.listings import approve, reject

    ids = body.ids
    if not ids:
        ids = [r.id for r in db.query(PortalListing.id)
               .filter(PortalListing.status == "pending").all()]

    applied = rejected = skipped = 0
    reasons: list[str] = []
    for lid in ids:
        if body.approve:
            ok, why = approve(db, lid, user_id=admin.id)
            if ok:
                applied += 1
            else:
                skipped += 1
                if why not in reasons and len(reasons) < 10:
                    reasons.append(why)
        else:
            rejected += 1 if reject(db, lid, user_id=admin.id) else 0
    return DecideOut(applied=applied, rejected=rejected, skipped=skipped,
                     reasons=reasons)


@router.get("/release/listings/sold", response_model=NewListingsOut)
def new_sales(limit: int = 200, admin: User = Depends(require_admin),
              db: Session = Depends(get_db)) -> NewListingsOut:
    """Sales the weekly sweep found that our own sold files have not got.

    Held to a higher bar than a listing, and deliberately. A wrong asking price
    costs one listing; a wrong SALE price poisons a whole suburb — it feeds the
    $/m² rate and the sale/CV ratio every valuation leans on. So each row
    carries a flag when its price sits far from what that suburb has been doing,
    measured against the suburb's own sales rather than a global rule.
    """
    from ..models import PortalListing
    from ..portals.listings import pending as pending_listings

    rows = pending_listings(db, kind="sold", limit=limit)
    total = (db.query(PortalListing)
             .filter(PortalListing.status == "pending",
                     PortalListing.kind == "sold").count())
    return NewListingsOut(
        pending=total,
        listings=[
            NewListingOut(
                id=r.id, source=r.source, url=r.url, address=r.address,
                suburb=r.suburb, property_type=r.property_type,
                # The sale price is the headline for a sale, so it goes where
                # the asking price sits for a listing.
                price_numeric=r.sale_price, price_display=r.sale_method,
                cv_numeric=r.cv_numeric, floor_area_m2=r.floor_area_m2,
                land_area_m2=r.land_area_m2, beds=r.beds, baths=r.baths,
                listed_date=r.sold_date, image_url=r.image_url,
                has_council_data=bool(r.cv_numeric),
                price_flag=r.price_flag,
            )
            for r in rows
        ],
    )


@router.post("/release/listings/sweep-sold", response_model=StageStarted)
def sweep_sold_endpoint(cap: int = 1000, admin: User = Depends(require_admin),
                        db: Session = Depends(get_db)) -> StageStarted:
    """Ask the portals what has sold. A job, for the same reason as above —
    this one asks for a thousand rows and takes longer, not less."""
    from ..portals.apify import token

    if not token(db):
        raise HTTPException(
            status_code=400,
            detail="No Apify token yet — add one in the Data connection panel.")
    # No batch: a sweep is looking for properties that are not in one yet. The
    # column is a NULLABLE foreign key, so None is the only correct value — 0
    # would violate the constraint on Postgres and pass on SQLite, which is the
    # worst possible combination.
    job = create_stage_job(db, stage="listings", batch_id=None,
                           region="Auckland", uploaded_by_id=admin.id)
    jid = job.id
    threading.Thread(target=_run_sweep_job, args=(jid,),
                     kwargs={"kind": "sold", "hours": 24, "cap": cap},
                     daemon=True).start()
    return StageStarted(job_id=jid, batch_id=0, stage="listings")


# ---- the Apify connection ----------------------------------------------------
class ApifyStatus(BaseModel):
    configured: bool
    # "environment" (set in Railway) or "panel" (typed in here). Worth saying,
    # because the environment wins and someone changing the wrong one and
    # seeing nothing happen is a bad afternoon.
    source: str | None = None
    # Never the token. Enough to recognise which one is saved.
    last_four: str | None = None
    ok: bool | None = None
    message: str | None = None
    # True when the environment holds it, so the form says so rather than
    # letting someone overwrite a value that will not be used.
    locked: bool = False


class ApifyTokenIn(BaseModel):
    token: str


@router.get("/release/apify", response_model=ApifyStatus)
def apify_status(test: bool = False, admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)) -> ApifyStatus:
    """Is there a token, where did it come from, and does it work?

    `test=true` asks Apify. That is a real network call, so it is not what a
    page load does — the panel tests on demand and when a token is saved.
    """
    import os

    from ..models import APIFY_TOKEN
    from ..portals.apify import check, token
    from ..settings_store import get as get_setting

    from_env = bool((os.getenv("APIFY_TOKEN") or "").strip())
    stored = get_setting(db, APIFY_TOKEN)
    tok = token(db)

    out = ApifyStatus(
        configured=bool(tok),
        source=("environment" if from_env else "panel" if stored else None),
        last_four=(tok[-4:] if tok and len(tok) >= 4 else None),
        locked=from_env,
    )
    if test and tok:
        out.ok, out.message = check(tok)
    elif not tok:
        out.ok, out.message = False, "No Apify token yet"
    return out


@router.post("/release/apify", response_model=ApifyStatus)
def save_apify_token(body: ApifyTokenIn, admin: User = Depends(require_admin),
                     db: Session = Depends(get_db)) -> ApifyStatus:
    """Save a token, after checking it works.

    Tested BEFORE it is stored. A token that does not work is not worth keeping,
    and the failure people actually hit is a real token on an account with no
    credit — which looks exactly like a working one until the first sweep comes
    back with nothing.

    Encrypted at rest with the same key as the assistant's, because a token
    typed into a browser must not be readable by anyone who gets a look at the
    database. Send an empty string to remove it.
    """
    from ..assistant import keys
    from ..models import APIFY_TOKEN
    from ..portals.apify import check
    from ..settings_store import SettingsUnavailable
    from ..settings_store import put as put_setting

    tok = (body.token or "").strip()
    if not tok:
        try:
            put_setting(db, APIFY_TOKEN, None, by=admin.id)
        except SettingsUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        return ApifyStatus(configured=False, ok=False, message="Token removed")

    ok, message = check(tok)
    if not ok:
        # Refused rather than saved. Storing a token we already know is dead
        # only moves the discovery to the first sweep.
        return ApifyStatus(configured=False, ok=False, message=message)

    try:
        put_setting(db, APIFY_TOKEN, keys.encrypt(tok), by=admin.id)
    except SettingsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return ApifyStatus(configured=True, source="panel", last_four=tok[-4:],
                       ok=True, message=message)


# ---- the trained valuation ---------------------------------------------------
#
# Everything above prices listings with coefficients that came out of a
# spreadsheet dated 17 May 2026. They were fitted once, elsewhere, on data we do
# not hold, and they have never moved. Sold files have landed every week since
# and taught them nothing.
#
# These endpoints fit a valuation on OUR sales, judge it against both the
# council figure and the estimator already running, and keep it with the numbers
# that judged it. The judging is the point: a model does not get to price
# anything because it is new.
#
# Training runs as a background job for the same reason the portal sweep does —
# a fit over 50,000 sales with five-fold cross-validation takes tens of seconds,
# and holding an HTTP request open for that gets it cut off by the proxy and
# reported as "500 with no response body".

class TrainedModelOut(BaseModel):
    id: int
    trained_at: str | None
    n_train: int | None
    n_test: int | None
    forward_error: float | None
    engine_error: float | None
    raw_cv_error: float | None
    shipped: bool
    is_active: bool
    verdict: str | None

    class Config:
        from_attributes = True


class MLStatusOut(BaseModel):
    """What is fitted, what is live, and whether it is allowed to price."""
    has_model: bool
    enabled: bool
    active: TrainedModelOut | None
    history: list[TrainedModelOut]
    sold_rows_available: int


def _model_out(r) -> "TrainedModelOut":
    return TrainedModelOut(
        id=r.id,
        trained_at=r.trained_at.isoformat() if r.trained_at else None,
        n_train=r.n_train, n_test=r.n_test,
        forward_error=r.forward_error, engine_error=r.engine_error,
        raw_cv_error=r.raw_cv_error, shipped=r.shipped, is_active=r.is_active,
        verdict=r.verdict)


@router.get("/ml/status", response_model=MLStatusOut)
def ml_status(_: User = Depends(require_admin),
              db: Session = Depends(get_db)) -> MLStatusOut:
    from sqlalchemy import func as _func

    from ..ml import store as ml_store
    from ..models import PropertySold

    rows = db.query(_func.count(PropertySold.id)).filter(
        PropertySold.sale_price.isnot(None)).scalar() or 0
    active = ml_store.active_row(db)
    # "enabled" as a person reads it: is the trained valuation actually pricing
    # listings right now. Not overridden off AND something fitted to use. The
    # override alone would show "on" with nothing to run, which is an afternoon
    # wondering why no price moved.
    return MLStatusOut(
        has_model=active is not None,
        enabled=bool(active is not None and ml_store.enabled(db)),
        active=_model_out(active) if active else None,
        history=[_model_out(r) for r in ml_store.history(db, limit=12)],
        sold_rows_available=int(rows),
    )


@router.post("/ml/train")
def ml_train(region: str = "Auckland", user: User = Depends(require_admin),
             db: Session = Depends(get_db)) -> dict:
    """Fit a valuation on our own sales. Returns a job id; poll /api/admin/jobs.

    The fit itself decides whether the result is used — see
    app/ml/evaluate.should_ship. Nothing here can override that, deliberately:
    a button that ships a model regardless of its measured accuracy is a button
    that will eventually be pressed.
    """
    import threading

    from ..staged_stages import _update, create_stage_job

    job = create_stage_job(db, stage="ml-train", batch_id=None, region=region,
                           uploaded_by_id=user.id)
    job_id = job.id
    uid = user.id

    def _work() -> None:
        from ..db import SessionLocal
        from ..ml import store as ml_store
        from ..reprice import _sold_df

        with SessionLocal() as s:
            try:
                _update(s, job_id, status="running", stage="reading sales",
                        progress_pct=10)
                sold = _sold_df(s, region)
                if sold is None or sold.empty:
                    raise ValueError(f"no sold data loaded for {region}")
                _update(s, job_id, status="running",
                        stage=f"fitting on {len(sold):,} sales", progress_pct=35)
                row = ml_store.train_and_store(s, sold, user_id=uid)
                _update(s, job_id, status="completed", progress_pct=100,
                        rows_inserted=row.n_train or 0,
                        stage=(row.verdict or "")[:64])
            except Exception as exc:                       # noqa: BLE001
                _update(s, job_id, status="failed", stage="done",
                        error_message=f"{type(exc).__name__}: {str(exc)[:300]}")

    threading.Thread(target=_work, daemon=True).start()
    return {"job_id": job_id}


class MLEnableIn(BaseModel):
    enabled: bool


@router.post("/ml/enabled", response_model=MLStatusOut)
def ml_set_enabled(body: MLEnableIn, user: User = Depends(require_admin),
                   db: Session = Depends(get_db)) -> MLStatusOut:
    """Turn the trained valuation on or off for pricing.

    Refuses to turn on with nothing fitted. Silently enabling a switch that
    does nothing is how you spend an afternoon wondering why prices did not
    move.
    """
    from ..ml import store as ml_store

    if body.enabled and ml_store.active_row(db) is None:
        raise HTTPException(
            400, "There is no trained model to use yet — train one first.")
    ml_store.set_enabled(db, body.enabled, user_id=user.id)
    return ml_status(_=user, db=db)


@router.post("/ml/rollback/{model_id}", response_model=MLStatusOut)
def ml_rollback(model_id: int, user: User = Depends(require_admin),
                db: Session = Depends(get_db)) -> MLStatusOut:
    from ..ml import store as ml_store

    try:
        ml_store.rollback_to(db, model_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return ml_status(_=user, db=db)


class AccuracyOut(BaseModel):
    """Ours vs Hougarden vs the council figure, by property type."""
    rows: list[dict]
    overall: dict | None
    trained_on: int | None = None
    tested_from_month: float | None = None
    min_rows: int | None = None
    reason: str | None = None
    method: str | None = None


@router.get("/ml/accuracy", response_model=AccuracyOut)
def ml_accuracy(region: str = "Auckland", _: User = Depends(require_admin),
                db: Session = Depends(get_db)) -> AccuracyOut:
    """How close each method gets to the price a house actually sold at.

    Computed rather than cached, and it is not cheap — it fits a valuation on
    the older sales to score the newer ones honestly. Called from a panel that
    loads it once, not from anything on a hot path.
    """
    from ..ml.accuracy import compare
    from ..reprice import _sold_df

    sold = _sold_df(db, region)
    if sold is None or sold.empty:
        return AccuracyOut(rows=[], overall=None,
                           reason=f"no sold data loaded for {region}")
    return AccuracyOut(**compare(sold))


# ---- pulling the data out so someone can look at it -------------------------
#
# The trained valuation moved live prices by -45% to +350% and it could not be
# reproduced outside production: the committed fixture carries no bedroom or
# bathroom counts, so on it the model shifts valuations by under 2%. Every
# diagnosis was a guess about data nobody outside the running system could see.
#
# These two endpoints end that. The summary answers "why are only nine
# underpriced" in a paragraph; the CSV carries every input, the published price,
# and what the model WANTED to do before the bound stopped it.

@router.get("/ml/diagnostic")
def ml_diagnostic(region: str = "Auckland", _: User = Depends(require_admin),
                  db: Session = Depends(get_db)) -> dict:
    """The short version: counts, and how far the model is moving things."""
    from ..ml.diagnostic import summary
    return summary(db, region=region)


@router.get("/ml/diagnostic.csv")
def ml_diagnostic_csv(region: str = "Auckland", batch_id: int | None = None,
                      limit: int = 25_000, _: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    """Every live listing, its inputs, its price, and the model's raw opinion.

    Property data only — public listing addresses, council figures, areas, room
    counts. No user, no email, no saved search, no key. It is meant to be sent
    to someone for help, so it has to be safe to send.
    """
    from fastapi.responses import Response

    from ..ml.diagnostic import to_csv

    body = to_csv(db, region=region, batch_id=batch_id, limit=limit)
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="apex_pricing_diagnostic_{region}.csv"'},
    )
