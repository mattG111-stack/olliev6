"""What it was listed at last week.

A house advertised at $1,250,000 that comes back in the next load "by
negotiation" is not a house with no price. It is a house whose vendor has
stopped naming one — which in this market almost always means the number is
coming down, not up. Yet it fell straight out of the deal feed, because the only
figure the feed carries for those listings is a search price: the number an
agent sets so the listing appears in buyers' filters, deliberately low, and
never what they would accept. Measuring a margin against it manufactures a
discount. So the pipeline throws it away, correctly, and the listing goes dark.

On the last batch that was 455 listings, and we had a real advertised price for
a large share of them seven days earlier.

This carries that price forward. For every listing in the new load with no
advertised price, it finds the most recent EARLIER load that advertised one for
the same address and stamps it on the row along with the date it was seen. The
pipeline then works from that price less NEGOTIATION_DISCOUNT_PCT, rounded to
the nearest $10,000, and every surface that shows the number also shows where it
came from.

Two things it deliberately will not do:

  IT NEVER OVERRIDES A REAL PRICE. If the vendor is advertising a price today,
  that is the price. The carry-forward only fills a gap.

  IT NEVER CARRIES A PRICE THAT WAS NOT REAL. A prior row whose "price" was
  itself a placeholder — the scraper filling in the council valuation or the
  last sale — is not a price to carry, and carrying it would launder a guess
  into a fact by moving it between two loads.
"""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

# The address key was written out twice and the carry-forward had the weaker
# copy — see app/addresses.py for what that cost. One definition now.
from .addresses import address_key
from .models import BatchType, ImportBatch, PropertyForSale

# What a vendor who has stopped naming a price is really asking, relative to the
# last number they did name. A price withdrawn is a price softening; this is the
# house's own last advertised figure, discounted.
#
# A percentage rather than a flat amount, so it scales with the house: $50,000
# off a $600,000 unit is a different move from $50,000 off a $3M home, and only
# one of them is a realistic softening.
NEGOTIATION_DISCOUNT_PCT = 0.03

# ...then rounded to the nearest $10,000, because that is how a price is spoken.
# 3% of $1,250,000 is $1,212,500, and no agent in the country says that number.
# Rounding it is not a loss of precision — the precision was never there. It is
# a derived figure, and printing it to the dollar claims a confidence we do not
# have.
ROUND_TO = 10_000.0

# The sentence that travels with the number, everywhere it is shown.
DERIVED_BASIS = f"last advertised, less {NEGOTIATION_DISCOUNT_PCT:.0%}"
ADVERTISED_BASIS = "advertised"


def derived_asking(prior: float | None) -> float | None:
    """The working price for a listing whose vendor has withdrawn theirs.

    None when there is nothing honest to return: no prior price, or one so small
    that the discount and the rounding leave nothing behind. A zero or negative
    asking would sort to the top of every deal list in the app.
    """
    try:
        p = float(prior or 0.0)
    except (TypeError, ValueError):
        return None
    # NaN and infinity both. round(inf) raises OverflowError, and this runs in a
    # loop over the batch, so one poisoned prior price would end the carry-
    # forward for every listing behind it rather than for itself.
    if p <= 0 or p != p or p in (float("inf"), float("-inf")):
        return None
    out = round(p * (1.0 - NEGOTIATION_DISCOUNT_PCT) / ROUND_TO) * ROUND_TO
    return out if out > 0 else None


def is_placeholder_price(asking, cv, last_sold) -> bool:
    """Is this "price" the scraper filling a blank rather than a vendor naming one?

    A by-negotiation listing arrives with the council valuation copied into the
    price field, to the dollar, or with the last sale price. Both are guesses
    wearing a price's clothes.

    ONE rule, three callers — this module, pricing.pipeline and release. It used
    to be written out separately in each, which is how the pipeline came to
    treat 491 listings as priced while release held the same 491 as "no real
    asking": two spellings of one idea, disagreeing.
    """
    try:
        a = float(asking or 0.0)
    except (TypeError, ValueError):
        return False
    if a <= 0 or a != a:
        return False
    try:
        c = float(cv or 0.0)
    except (TypeError, ValueError):
        c = 0.0
    if c > 0 and abs(a - c) < 0.005 * c:
        return True
    try:
        ls = float(last_sold or 0.0)
    except (TypeError, ValueError):
        ls = 0.0
    return bool(ls > 0 and abs(a - ls) < 1.0)


def needs_a_carried_price(p: PropertyForSale) -> bool:
    """Does this listing lack a price a vendor actually named?

    Not the same as "has no price", which is what this asked at first and why
    the feature rescued nothing. On a real load only 21 listings arrive with the
    price field empty. The 491 that need this are the ones whose price field
    holds the council valuation verbatim — they have a number, it is just not a
    price, and they are precisely the rows held as "By negotiation - no real
    asking". Asking "is the field empty" skips every one of them.
    """
    if p.asking_basis and p.asking_basis != ADVERTISED_BASIS:
        return True                        # already carried; re-derive the same
    if not (p.asking_price and p.asking_price > 0):
        return True
    return is_placeholder_price(p.asking_price, p.cv_numeric,
                                p.valuation_last_sold_value)


def _was_really_advertised(p: PropertyForSale) -> bool:
    """Did a vendor actually name this number? The mirror of the above, asked of
    the OLD row: only a price a vendor named is worth carrying forward, and
    moving a placeholder between two loads would launder a guess into a fact."""
    if not (p.asking_price and p.asking_price > 0):
        return False
    if p.asking_basis and p.asking_basis != ADVERTISED_BASIS:
        return False                       # already derived; do not derive twice
    return not is_placeholder_price(p.asking_price, p.cv_numeric,
                                    p.valuation_last_sold_value)


def carry_forward_prices(db: Session, batch_id: int, region: str = "Auckland") -> int:
    """Stamp the last advertised price on every priceless listing in this batch.

    Returns how many were filled. Idempotent: re-running finds the same prior
    price and writes the same values.
    """
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        return 0

    # Which rows need one: no price a VENDOR named. A row genuinely advertising a
    # price today is left completely alone.
    needy = [p for p in db.query(PropertyForSale)
             .filter(PropertyForSale.import_batch_id == batch_id)
             if needs_a_carried_price(p)]
    if not needy:
        return 0
    wanted = {}
    for p in needy:
        k = address_key(p.address, p.suburb)
        if k:
            wanted.setdefault(k, []).append(p)
    if not wanted:
        return 0

    # Every earlier for-sale load in this region, newest first — the first one
    # that advertised a price for an address is the one that counts.
    earlier = (db.query(ImportBatch)
               .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                       ImportBatch.region == region,
                       ImportBatch.id != batch_id,
                       or_(ImportBatch.created_at <= batch.created_at,
                           ImportBatch.created_at.is_(None)))
               .order_by(ImportBatch.id.desc()).all())

    filled = 0
    still = dict(wanted)
    for prev in earlier:
        if not still:
            break
        for old in (db.query(PropertyForSale)
                    .filter(PropertyForSale.import_batch_id == prev.id,
                            PropertyForSale.asking_price.isnot(None))):
            k = address_key(old.address, old.suburb)
            if k not in still or not _was_really_advertised(old):
                continue
            when = prev.created_at or old.created_at
            for p in still.pop(k):
                p.prior_asking_price = float(old.asking_price)
                p.prior_asking_seen_at = when
                filled += 1
    db.commit()
    return filled
