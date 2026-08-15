"""Two-stage weekly publish: stage → review → publish, with per-row holds.

Uploads land as STAGED batches (not live). This module scores the staged data,
holds the flagged rows back, summarises everything for review, and — on the
admin's confirmation — publishes the release atomically. Held rows stay hidden
until an admin fixes and publishes them individually.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import BatchType, ImportBatch, PropertyForSale

# Section/bare-land types legitimately have no floor area — never hold those for
# a missing floor. (Mirror of routers.properties._SECTION_TYPES.)
_SECTION_TYPES = ("建地", "乡村住宅建地", "土地", "地皮", "Section", "Vacant land", "Land")

# Deal-margin floor. The system only carries listings with a real edge: at least
# this many dollars of independent fair value over the asking price. Anything
# below it — including rows we couldn't price at all, so have no margin — is held
# out of the customer feed (still in the DB, viewable, and individually enrich /
# re-priceable). Keeps the live set to the ~1-in-5 listings worth acting on.
MARGIN_MIN_DOLLARS = 10_000.0
BELOW_MARGIN_REASON = f"Below ${int(MARGIN_MIN_DOLLARS):,} margin"
NO_ASKING_REASON = "By negotiation — no real asking"
NO_PRICE_REASON = "No advertised price ({kind})"


def _asking_is_placeholder(p: PropertyForSale) -> bool:
    """True when the scraped "asking" is not a real list price — the scraper filled
    a by-negotiation listing from the CV (asking == CV to the dollar) or from the
    prior sale (asking == last-sold, exact). Mirrors pipeline.asking_is_placeholder.
    Without this the guessed list price (e.g. 6 Pekanga Rd: "$580k" == CV, valued
    $1.62M) produces a fake $1M margin that would sneak the row into the feed."""
    ask, cv, ls = p.asking_price, p.cv_numeric, p.valuation_last_sold_value
    if not ask:
        return False
    if cv and abs(ask - cv) < 0.005 * cv:
        return True
    if ls and abs(ask - ls) < 1.0:
        return True
    return False


def _below_margin(p: PropertyForSale) -> bool:
    """True if this listing lacks a $MARGIN_MIN_DOLLARS fair-value-over-asking edge
    (or couldn't be priced, so has no fair value / asking to compare)."""
    fv, ask = p.fair_value, p.asking_price
    if fv is None or ask is None:
        return True
    return (fv - ask) < MARGIN_MIN_DOLLARS


# ---- holding flagged rows ---------------------------------------------------
def _hold_reason(p: PropertyForSale) -> str | None:
    """Why this listing should be held from publishing, or None if it's clean."""
    if p.land_area_flag:
        return f"Land area flagged ({p.land_area_flag})"
    if p.cv_flag == "suspect":
        return "CV looks wrong vs the local market"
    if p.floor_area_m2 is None and (p.property_type not in _SECTION_TYPES):
        return "Missing floor area"
    # Pipeline couldn't price it confidently: land-only / incomplete CV with no
    # size-controlled sold comps (a new build we can only value off much larger
    # homes). Held rather than shown to a customer with a number we can't defend.
    if p.expected_sale_path == "insufficient_comps":
        return "Not enough comparable sales to price confidently"
    # No real asking (scraper copied the CV or the last sale). Held BEFORE the
    # margin check: fair_value − a placeholder asking is a fake margin that would
    # otherwise mark it a deal (6 Pekanga Rd valued $1.62M vs a guessed $580k ask).
    # We can't call it a deal without a real list price — the valuation overrules.
    # NOTE: a listing with no advertised price is NOT held here. It stays in the
    # feed as a plain listing with a valuation; what it never gets is a deal
    # signal, and that is suppressed upstream in the pipeline (deal_value) so
    # nothing can sort or filter on a margin measured against a search price.
    #
    # To hold them out of the feed altogether instead, return
    # NO_PRICE_REASON.format(kind=_lt) for any _lt that is set and not "fixed".
    # Left as one line because it is a product call, not a technical one, and it
    # would remove a large share of the Auckland market from the listing count.
    if _asking_is_placeholder(p):
        return NO_ASKING_REASON
    # Data is fine, but there's no deal here — hold it out of the feed. Checked
    # last so a genuine data problem is reported ahead of "just not a deal".
    if _below_margin(p):
        return BELOW_MARGIN_REASON
    return None


def hold_flagged_rows(db: Session, batch_id: int | None) -> int:
    """Mark every flagged row in a staged batch as held. Returns how many held."""
    if not batch_id:
        return 0
    held = 0
    for p in db.query(PropertyForSale).filter(PropertyForSale.import_batch_id == batch_id):
        reason = _hold_reason(p)
        if reason:
            p.is_held = True
            p.hold_reason = reason
            held += 1
        else:
            p.is_held = False
            p.hold_reason = None
    db.commit()
    return held


# ---- review summary ---------------------------------------------------------
@dataclass
class StagedSummary:
    has_staged: bool = False
    sold_batch_id: int | None = None
    forsale_batch_id: int | None = None
    sold_rows: int = 0
    forsale_rows: int = 0
    forsale_rejected: int = 0
    held_total: int = 0
    hold_reasons: dict[str, int] = field(default_factory=dict)
    pv_checked: int = 0
    pv_pending: int = 0
    uploaded_at: str | None = None


def _staged_batch(db: Session, batch_type: str, region: str) -> ImportBatch | None:
    return (db.query(ImportBatch)
            .filter(ImportBatch.batch_type == batch_type,
                    ImportBatch.region == region,
                    ImportBatch.status == "staged")
            .order_by(ImportBatch.id.desc()).first())


def staged_summary(db: Session, region: str = "Auckland") -> StagedSummary:
    sold = _staged_batch(db, BatchType.SOLD.value, region)
    fs = _staged_batch(db, BatchType.FOR_SALE.value, region)
    s = StagedSummary(
        has_staged=bool(sold or fs),
        sold_batch_id=sold.id if sold else None,
        forsale_batch_id=fs.id if fs else None,
        sold_rows=sold.rows_inserted if sold else 0,
        forsale_rejected=fs.rows_rejected if fs else 0,
        uploaded_at=(fs or sold).created_at.isoformat() if (fs or sold) and (fs or sold).created_at else None,
    )
    if fs:
        base = db.query(PropertyForSale).filter(PropertyForSale.import_batch_id == fs.id)
        s.forsale_rows = base.count()
        s.held_total = base.filter(PropertyForSale.is_held.is_(True)).count()
        rows = (db.query(PropertyForSale.hold_reason, func.count(PropertyForSale.id))
                  .filter(PropertyForSale.import_batch_id == fs.id, PropertyForSale.is_held.is_(True))
                  .group_by(PropertyForSale.hold_reason).all())
        s.hold_reasons = {r[0] or "other": r[1] for r in rows}
        s.pv_checked = base.filter(PropertyForSale.pv_checked_at.isnot(None)).count()
        s.pv_pending = s.forsale_rows - s.pv_checked
    return s


# ---- publish ----------------------------------------------------------------
def publish_release(db: Session, region: str = "Auckland") -> dict:
    """Promote the staged sold + for-sale batches to live, atomically. The old
    live batches are archived. Held rows stay held (hidden) but ride along in the
    now-live batch so they can be fixed and published later."""
    now = datetime.now(timezone.utc)
    published = []
    for bt in (BatchType.SOLD.value, BatchType.FOR_SALE.value):
        staged = _staged_batch(db, bt, region)
        if not staged:
            continue
        # Archive whatever is live for this type + region.
        for prior in (db.query(ImportBatch)
                        .filter(ImportBatch.batch_type == bt, ImportBatch.region == region,
                                ImportBatch.is_active.is_(True)).all()):
            prior.is_active = False
            prior.status = "archived"
        staged.is_active = True
        staged.status = "published"
        staged.published_at = now
        published.append({"batch_type": bt, "batch_id": staged.id})
    db.commit()
    return {"published": published, "count": len(published)}
