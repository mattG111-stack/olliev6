"""Where did the deals go?

"There are only 9 underpriced houses in all of Auckland" is not a number anyone
can act on, because it does not say WHICH step threw the other two thousand
listings away. A batch of 2,141 becomes a handful through nine separate gates,
each one defensible on its own, and until you can see the count after every gate
you are guessing at which one is misbehaving.

This walks the live batch through those gates IN THE ORDER THE SYSTEM APPLIES
THEM and reports what is left after each. It reads stored columns only — it does
not re-price anything — so what it reports is what the app is actually serving,
not what a re-run would produce.

The last figure is the one that matters. `mismatch` counts rows that pass every
single test the deal rule applies and are STILL not flagged. That number should
be zero. If it isn't, the flag on disk disagrees with the data on disk, which
means the rows were written by a different run from the one that priced them —
a re-price, a partial ingest, an interrupted publish — and no amount of staring
at the pricing code will explain it, because the pricing code is not what's
wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .models import PropertyForSale
from .pricing.buyprice import _title_bucket
from .release import MARGIN_MAX_PCT, _SECTION_TYPES, _asking_is_placeholder

# The deal rule's own threshold — a listing is a deal when the independent
# valuation sits at least this far above the asking price. Mirrors
# pricing.scoring.signals(); kept as a named constant so the funnel and the rule
# can never drift into describing different populations.
DEAL_MARGIN = 0.05


@dataclass
class Step:
    """One gate: what it keeps, what it cost, and why it exists."""
    label: str
    kept: int
    lost: int
    why: str


@dataclass
class DealFunnel:
    batch_id: int | None = None
    total: int = 0
    steps: list[Step] = field(default_factory=list)
    # Rows that satisfy every condition of the deal rule but carry
    # is_underpriced = False. Should always be 0.
    mismatch: int = 0
    mismatch_examples: list[str] = field(default_factory=list)
    # The reverse: flagged, but the stored numbers no longer support it.
    orphan_flags: int = 0
    # Why the held rows are held, biggest first.
    hold_reasons: list[tuple[str, int]] = field(default_factory=list)
    # What the app actually reports.
    flagged: int = 0


def _visible(p: PropertyForSale) -> bool:
    """The browse-list rule, row by row — mirrors routers.properties._hide_bad_data."""
    if p.is_held:
        return False
    if _asking_is_placeholder(p):
        return False
    if p.margin is not None and p.margin > MARGIN_MAX_PCT:
        return False
    if p.floor_area_m2 is None and p.property_type not in _SECTION_TYPES:
        return False
    return True


def deal_funnel(db: Session, batch_id: int | None) -> DealFunnel:
    if not batch_id:
        return DealFunnel()

    rows = list(db.query(PropertyForSale)
                .filter(PropertyForSale.import_batch_id == batch_id))
    f = DealFunnel(batch_id=batch_id, total=len(rows))

    # Held rows first, with the reasons spelled out — this is normally the
    # single largest drop, and "held" covers everything from a missing floor
    # area to "the margin just isn't there", which are not the same problem.
    counts: dict[str, int] = {}
    for p in rows:
        if p.is_held:
            counts[p.hold_reason or "held, no reason recorded"] = \
                counts.get(p.hold_reason or "held, no reason recorded", 0) + 1
    f.hold_reasons = sorted(counts.items(), key=lambda kv: -kv[1])

    gates = [
        ("still shown after the review holds", _visible,
         "held back in review, or hidden as incomplete/placeholder data"),
        ("has an advertised asking price", lambda p: bool(p.asking_price and p.asking_price > 0),
         "no price advertised, so there is nothing to measure a discount against"),
        ("we produced a valuation", lambda p: bool(p.fair_value and p.fair_value > 0),
         "could not be valued with enough confidence to publish a number"),
        ("the valuation counts as a deal signal", lambda p: p.margin is not None,
         "valued, but the valuation is not allowed to claim a deal — auction or "
         "by-negotiation price, a guide price, a stale listing, or a discount too "
         "large to believe"),
        (f"worth at least {int(DEAL_MARGIN * 100)}% more than the asking",
         lambda p: (p.margin or 0) >= DEAL_MARGIN,
         "priced about right — the edge is inside the noise"),
        ("confident enough to say so",
         lambda p: (p.confidence or "").lower() in ("medium", "high"),
         "too few comparable sales nearby to stand behind the call"),
        ("not leasehold", lambda p: _title_bucket(p.type_of_title) != "LH",
         "leasehold trades on terms none of our inputs capture"),
    ]

    live = rows
    for label, test, why in gates:
        before = len(live)
        live = [p for p in live if test(p)]
        f.steps.append(Step(label=label, kept=len(live), lost=before - len(live), why=why))

    # Everything above IS the deal rule. Anything still standing should carry
    # the flag; anything that doesn't was written by a different run.
    bad = [p for p in live if not p.is_underpriced]
    f.mismatch = len(bad)
    f.mismatch_examples = [
        f"{p.address or 'address missing'} — asking ${p.asking_price:,.0f}, "
        f"valued ${p.fair_value:,.0f} ({(p.margin or 0) * 100:+.1f}%), "
        f"{(p.confidence or 'no').lower()} confidence"
        for p in sorted(bad, key=lambda p: -(p.margin or 0))[:10]
    ]

    ok = {id(p) for p in live}
    f.orphan_flags = sum(
        1 for p in rows if p.is_underpriced and _visible(p) and id(p) not in ok)

    # The funnel's own bottom line: of the rows that cleared every gate, how many
    # actually carry the flag. This is deliberately NOT the app's count — the app
    # also shows `orphan_flags`, rows flagged without the numbers to back it, and
    # adding those in here would make the last step larger than the one above it
    # and hide the very disagreement this is built to find.
    f.flagged = sum(1 for p in rows if p.is_underpriced and _visible(p))
    f.steps.append(Step(
        label="and is flagged as a deal",
        kept=len(live) - f.mismatch,
        lost=f.mismatch,
        why="passes every test above but the stored flag says no — the flag and "
            "the numbers beside it were written by different runs"))
    return f
