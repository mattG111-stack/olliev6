"""Two-stage weekly publish: stage → review → publish, with per-row holds.

Uploads land as STAGED batches (not live). This module scores the staged data,
holds the flagged rows back, summarises everything for review, and — on the
admin's confirmation — publishes the release atomically. Held rows stay hidden
until an admin fixes and publishes them individually.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .models import BatchType, ImportBatch, PropertyForSale, PropertySold
from .pricing.pipeline import is_land_only_cv
from .prior_price import ADVERTISED_BASIS, is_placeholder_price
from .runlog import record as _record

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

# Deal-margin CEILING. There has only ever been a floor, so nothing bounded the
# top: whatever the worst data fault in a batch produced was free to publish, and
# the deal page picks its hero by taking the largest margin it can find.
#
# Measured before setting it, across 23 staged exports (86,804 rows carrying a
# margin), the margin itself is well behaved:
#   median +1.5%   90th +10.8%   95th +15.2%   99th +23.4%   max +79.9%
# ZERO rows sit above this line. That is deliberate and worth being honest about:
# this ceiling fixes nothing visible today. The number that actually ran away was
# "vs CV" — same 23 exports, 99th +123.9%, max +2,296.9% — and that is fixed at
# source in pricing.pipeline by not publishing a comparison against a CV we have
# already ruled land-only.
#
# So this is a backstop, not the fix: a fair value more than DOUBLE the asking is
# a broken input, not a find — nobody lists a house at half its worth — and it
# catches the next land-only CV that finds a route past the rule above. Held, not
# deleted: the row stays in the DB, viewable and re-priceable, and the reason
# names which check caught it.
MARGIN_MAX_PCT = 1.0
ABOVE_MARGIN_REASON = f"Margin above +{int(MARGIN_MAX_PCT * 100)}% — check the inputs"
LAND_ONLY_CV_REASON = "Council record values the land only"

# ---- which holds a lookup can actually fix ---------------------------------
#
# A hold means one of two completely different things, and treating them the
# same is what emptied the feed.
#
#   A DATA GAP    the row is missing something we could go and fetch — a floor
#                 area, a CV, a CV that looks wrong. CoreLogic exists for
#                 exactly these.
#   NOT A DEAL    the data is fine and the margin is not there. Spending a paid
#                 lookup on it buys nothing.
#
# The enrich stage skipped EVERY held row, on the reasoning that CoreLogic
# should be spent on deal candidates rather than the whole file. That reasoning
# is right about the second kind and exactly backwards about the first: a row
# held for "Missing floor area" was then never looked up, so its floor area was
# never filled, so it stayed held and unpriced permanently. The rows that most
# needed the lookup were the only ones guaranteed not to get it.
#
# Matched on the reason text because that is what is stored on the row; the
# constants above are the single source of it.
def hold_is_a_data_gap(reason: str | None) -> bool:
    """True when a lookup could plausibly clear this hold."""
    r = (reason or "").strip()
    if not r:
        return False
    if r in (LAND_ONLY_CV_REASON, "Missing floor area",
             "CV looks wrong vs the local market",
             "Not enough comparable sales to price confidently"):
        return True
    # "Land area flagged (…)" carries the flag in the text.
    return r.startswith("Land area flagged")


def _asking_is_placeholder(p: PropertyForSale) -> bool:
    """True when the scraped "asking" is not a real list price — the scraper filled
    a by-negotiation listing from the CV (asking == CV to the dollar) or from the
    prior sale (asking == last-sold, exact). Without this the guessed list price
    (e.g. 6 Pekanga Rd: "$580k" == CV, valued $1.62M) produces a fake $1M margin
    that would sneak the row into the feed.

    ONE rule, shared with pricing.pipeline and prior_price — it used to be
    written out separately in each, which is how the pipeline came to treat 491
    listings as priced while this held the same 491 as "no real asking".
    """
    # A price we derived ourselves has known provenance, so this guess about
    # provenance does not apply to it — and gets it wrong often enough to
    # matter, because rounding to the nearest $10,000 lands the figure on a
    # round council valuation. See pricing.pipeline for the worked example.
    if p.asking_basis and p.asking_basis != ADVERTISED_BASIS:
        return False
    return is_placeholder_price(p.asking_price, p.cv_numeric,
                                p.valuation_last_sold_value)


def _below_margin(p: PropertyForSale) -> bool:
    """True if this listing lacks a $MARGIN_MIN_DOLLARS fair-value-over-asking
    edge, or could not be priced at all.

    NO FAIR VALUE means held: there is nothing to show a customer.

    NO ASKING PRICE does not, and the difference matters more than it looks.
    Four listings in five sell by auction, tender or negotiation and have no
    asking price — and until the sale_method column was read, every one of them
    arrived carrying a number the scraper had put in the price field, so every
    one of them was measured against it and a great many passed. Now that the
    invented number is gone, treating "nothing to measure" as "fails the
    measurement" would take 71% of the Auckland market off the site in one
    publish: not because anything is wrong with those houses, but because their
    vendors have not named a price.

    So a listing with a valuation and no advertised price stays. What it does
    not get is a deal signal — no margin, nothing to sort or filter on — which
    is suppressed upstream in the pipeline. It is a house for sale with our
    valuation beside it, which is the truth about it.
    """
    fv, ask = p.fair_value, p.asking_price
    if fv is None:
        return True
    if ask is None:
        return False
    return (fv - ask) < MARGIN_MIN_DOLLARS


def _above_margin(p: PropertyForSale) -> bool:
    """True if the margin is too big to believe — see MARGIN_MAX_PCT.

    Measured against the asking price, the same way the published margin is, so
    what gets held is exactly what would have been shown.
    """
    fv, ask = p.fair_value, p.asking_price
    if fv is None or not ask or ask <= 0:
        return False                       # nothing to measure; other checks own it
    return (fv / ask - 1.0) > MARGIN_MAX_PCT


def _cv_is_land_only(p: PropertyForSale) -> bool:
    """The council record values the dirt and not the house standing on it.

    Same rule as the valuation path (pricing.pipeline.is_land_only_cv) — one
    definition, asked here against the stored row. A land-only CV poisons every
    CV-anchored number downstream, so the margin it produces is the gap between
    a built home and a bare section, not a deal.

    Sections and bare land legitimately have no improvement value, so they are
    exempt: their CV is the land, correctly.
    """
    if p.property_type in _SECTION_TYPES or p.property_type in ("Lifestyle Section",):
        return False
    return is_land_only_cv(p.cv_numeric, p.floor_area_m2, p.land_area_m2,
                           p.improvement_value_numeric, p.land_value_numeric)


# ---- holding flagged rows ---------------------------------------------------
def _hold_reason(p: PropertyForSale) -> str | None:
    """Why this listing should be held from publishing, or None if it's clean.

    NOTE on the first two checks: neither can fire on its own. `land_area_flag`
    and `cv_flag` are written only by scripts run by hand —
    scripts/verify_batch.py (fetches each listing's page and re-reads its land
    area) and scripts/reconcile_cv.py (weighs our CV against homes.co.nz's, with
    the market as referee). Nothing in the load → enrich → price → publish flow
    sets either one, so on a batch where those scripts were not run these two
    lines are no-ops and the protection they describe is not present.

    They are correct when the scripts HAVE run, so they stay. What was wrong was
    that this was invisible: a hold rule that silently never fires reads as
    protection you have. preflight._verification reports the coverage of both on
    the live batch so "this check is not running" shows up at /health/ready
    instead of being discovered by a bad row reaching a customer.
    """
    if p.land_area_flag:
        return f"Land area flagged ({p.land_area_flag})"
    if p.cv_flag == "suspect":
        return "CV looks wrong vs the local market"
    if p.floor_area_m2 is None and (p.property_type not in _SECTION_TYPES):
        return "Missing floor area"
    # The council valued the land and not the building. Every CV-anchored number
    # on the row inherits that, so the margin is a bare-section price against a
    # built-home valuation. Checked here as well as in the pipeline because a row
    # can be re-priced, hand-edited or portal-filled after pricing ran.
    if _cv_is_land_only(p):
        return LAND_ONLY_CV_REASON
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
    # Too good to be true. Held ahead of the floor so the message names the real
    # problem: a margin this size is an input fault we have not identified, not a
    # listing worth acting on. Without this the largest number in the batch was
    # always the most broken row, and it sorted to the top of the page.
    if _above_margin(p):
        return ABOVE_MARGIN_REASON
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
    # Every sold row in the system, not just this upload's.
    #
    # sold_rows counts the STAGED sold batch, so uploading a for-sale file on
    # its own shows "SOLD ROWS 0" — which reads as "the sold data is gone" when
    # it means "there is no sold file in this upload". Sold data accumulates
    # across published batches by design; a batch is a delivery, not the
    # dataset. This is the number that answers the question actually being
    # asked.
    sold_total: int = 0
    forsale_rows: int = 0
    forsale_rejected: int = 0
    held_total: int = 0
    hold_reasons: dict[str, int] = field(default_factory=dict)
    # How many listings in this batch a vendor has actually named a price for.
    #
    # The number that would have caught the auction fault before it went live,
    # and which nothing on the review screen said. One export had 1,436 of 1,586
    # carrying an "asking price"; the true figure was 308. Anybody who knows the
    # Auckland market would have stopped at 91%, because four houses in five
    # here sell by auction, tender or negotiation — but the count was never put
    # in front of them.
    #
    # Shown as a pair so the ratio is on the screen rather than in someone's
    # head, and stated before publishing rather than discovered afterwards.
    priced_rows: int = 0          # a vendor named a price
    unpriced_rows: int = 0        # auction / tender / by negotiation
    pv_checked: int = 0
    # Rows a lookup was ever wanted for — the honest denominator.
    pv_wanted: int = 0
    pv_pending: int = 0
    uploaded_at: str | None = None
    # Which of the two pre-live states this batch is in, so the page can show
    # one button rather than both and say what pressing it does. "staged" means
    # still being worked on; "preview" means finished and being checked while
    # the site still serves the previous load.
    stage: str | None = None


# The batch an operator is working on: loaded but not yet live. Both states are
# pre-live and everything on the review screen has to work on either, or moving
# to preview would take the grid, the Remove button and the re-price with it.
WORKING_STATUSES = ("staged", "preview")
# Everything a reader may draw on — the two above plus what is live. Sold comps
# in particular must not vanish from matching just because a batch moved on.
READABLE_STATUSES = ("staged", "preview", "published")


def _staged_batch(db: Session, batch_type: str, region: str) -> ImportBatch | None:
    return (db.query(ImportBatch)
            .filter(ImportBatch.batch_type == batch_type,
                    ImportBatch.region == region,
                    ImportBatch.status.in_(WORKING_STATUSES))
            .order_by(ImportBatch.id.desc()).first())


def staged_summary(db: Session, region: str = "Auckland") -> StagedSummary:
    sold = _staged_batch(db, BatchType.SOLD.value, region)
    fs = _staged_batch(db, BatchType.FOR_SALE.value, region)
    s = StagedSummary(
        has_staged=bool(sold or fs),
        sold_batch_id=sold.id if sold else None,
        forsale_batch_id=fs.id if fs else None,
        sold_rows=sold.rows_inserted if sold else 0,
        sold_total=int(
            db.query(func.count(PropertySold.id))
            .join(ImportBatch, ImportBatch.id == PropertySold.import_batch_id)
            .filter(ImportBatch.region == region,
                    ImportBatch.status.in_(("staged", "preview", "published")))
            .scalar() or 0),
        forsale_rejected=fs.rows_rejected if fs else 0,
        uploaded_at=(fs or sold).created_at.isoformat() if (fs or sold) and (fs or sold).created_at else None,
        # The for-sale batch decides, because it is the one being reviewed.
        stage=((fs or sold).status if (fs or sold) else None),
    )
    if fs:
        base = db.query(PropertyForSale).filter(PropertyForSale.import_batch_id == fs.id)
        s.forsale_rows = base.count()
        s.held_total = base.filter(PropertyForSale.is_held.is_(True)).count()
        rows = (db.query(PropertyForSale.hold_reason, func.count(PropertyForSale.id))
                  .filter(PropertyForSale.import_batch_id == fs.id, PropertyForSale.is_held.is_(True))
                  .group_by(PropertyForSale.hold_reason).all())
        s.hold_reasons = {r[0] or "other": r[1] for r in rows}
        s.priced_rows = base.filter(PropertyForSale.asking_price.isnot(None)).count()
        s.unpriced_rows = s.forsale_rows - s.priced_rows
        s.pv_checked = base.filter(PropertyForSale.pv_checked_at.isnot(None)).count()
        s.pv_pending = s.forsale_rows - s.pv_checked
        # How many rows a lookup was ever WANTED for. The enrich stage only asks
        # about rows missing a floor area, a land area or a CV — the rest have
        # everything the pricing needs and a paid lookup on them buys nothing.
        #
        # Counted because the review page read "93/2,141 enriched", and against
        # a denominator of every row in the batch a complete, successful run
        # looks like a 4% one. 93 of the 147 that needed it is a different
        # sentence entirely, and it is the true one.
        P = PropertyForSale
        _blank = lambda c: or_(c.is_(None), c == 0)          # noqa: E731
        s.pv_wanted = base.filter(or_(
            _blank(P.floor_area_m2), _blank(P.land_area_m2), _blank(P.cv_numeric),
        )).count()
    return s


# ---- publish ----------------------------------------------------------------
def send_to_preview(db: Session, region: str = "Auckland") -> dict:
    """Move the staged batches to PREVIEW — finished, inspectable, not live.

    Publishing used to be the only door and it was one-way: the moment it was
    pressed, whatever was wrong with the batch was wrong in front of customers,
    and the only way back was to re-upload the file.

    Nothing a customer sees changes here. is_active is untouched, so the site
    keeps serving the previous load while this one is looked over — which is the
    whole point of a second look. A listing that turns out not to be a real
    house can be removed while last week's data is still live.
    """
    now = datetime.now(timezone.utc)
    moved = []
    for bt in (BatchType.SOLD.value, BatchType.FOR_SALE.value):
        staged = (db.query(ImportBatch)
                  .filter(ImportBatch.batch_type == bt,
                          ImportBatch.region == region,
                          ImportBatch.status == "staged")
                  .order_by(ImportBatch.id.desc()).first())
        if not staged:
            continue
        staged.status = "preview"
        _record(db, stage="publish", event="sent_to_preview", batch_id=staged.id,
                count=staged.rows_inserted, commit=False,
                detail=(f"{staged.filename} moved to preview — "
                        f"{staged.rows_inserted or 0:,} "
                        f"{bt.replace('_', '-')} listings ready to check. The site "
                        f"is still showing the previous load."))
        moved.append({"batch_type": bt, "batch_id": staged.id})
    db.commit()
    return {"preview": moved, "count": len(moved), "at": now.isoformat()}


def publish_release(db: Session, region: str = "Auckland") -> dict:
    """Promote the previewed (or staged) sold + for-sale batches to live.

    Takes a batch in either pre-live state, so a build where preview is skipped
    still goes live rather than silently doing nothing. The old live batches are
    archived. Held rows stay held (hidden) but ride along in the now-live batch
    so they can be fixed and published later.
    """
    now = datetime.now(timezone.utc)
    published = []
    for bt in (BatchType.SOLD.value, BatchType.FOR_SALE.value):
        staged = _staged_batch(db, bt, region)
        if not staged:
            continue
        # Archive whatever is live for this type + region — but ONLY for
        # for-sale. That batch is a snapshot of what is on the market today, so
        # a new one genuinely supersedes the last.
        #
        # Sold is the opposite: it accumulates. Archiving the previous sold
        # batch here would undo the append on every publish — the comp loader
        # reads staged + published only, so last month's sales would drop out of
        # matching the moment this month's went live, and nothing would say so.
        if bt != BatchType.SOLD.value:
            for prior in (db.query(ImportBatch)
                            .filter(ImportBatch.batch_type == bt, ImportBatch.region == region,
                                    ImportBatch.is_active.is_(True)).all()):
                prior.is_active = False
                prior.status = "archived"
        staged.is_active = True
        staged.status = "published"
        staged.published_at = now
        _record(db, stage="publish", event="published", batch_id=staged.id,
                count=staged.rows_inserted, commit=False,
                detail=(f"{staged.filename} went live — {staged.rows_inserted or 0:,} "
                        f"{bt.replace('_', '-')} listings, replacing the previous load"))
        published.append({"batch_type": bt, "batch_id": staged.id})
    db.commit()
    return {"published": published, "count": len(published)}
