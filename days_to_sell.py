"""How long a house takes to sell, measured rather than read off a file.

    "we should have how many days to sell"
    "183 sold and you cant asnwer days too sell"

Both true, and the second one is the whole problem. The sold file carries a
days-on-market column and it is empty for almost everything in it: 89,262 of
116,959 rows are reconstructed out of sale-history JSON, and a history entry is
a price and a date. There was never a campaign behind it to measure. No amount
of care with that column produces a number, because the number is not in the
file.

So it is measured here instead, out of what we watch ourselves every week:

    first_seen_at   the week a listing first appeared in our book
    link_dead_at    the day the advertisement stopped answering

The difference between those two is a days-to-sell we observed first-hand. It
is the same thing an agent means by the phrase, and it is ours — nobody has to
supply it.

TWO HONEST LIMITS, STATED HERE SO NOBODY HAS TO REDISCOVER THEM:

1. An advertisement coming down is not a settlement. Most come down because the
   house sold; some are withdrawn or expire. This measures TIME ON MARKET,
   which is what the question actually means, but it is not a conveyancing
   record and must not be presented as one.

2. It starts empty and fills in. Nothing has a departure date until listings
   begin dropping off, so early on there is no figure at all. That is why
   `None` is a real answer below and never a zero — a fabricated zero is
   indistinguishable from a fast market, and would be believed.

`MIN_HOUSES` is the line between a median and an anecdote. Four houses in a
suburb is four houses; publishing their middle value as "days to sell in
Papakura" invents a market statistic out of a coincidence.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from .addresses import address_key
from .ingest import as_utc
from .models import BatchType, ImportBatch, PropertyForSale
from .pricing.glm import canonical_type

# Below this many completed listings there is no median worth printing.
MIN_HOUSES = 8
# A campaign shorter than a day is a data fault, not a fast sale. Two years is
# the same cut the sold-file figures use, so the two agree on what is stale.
MIN_DAYS = 1
MAX_DAYS = 730


@dataclass
class Observed:
    """A days-to-sell figure and everything needed to judge it."""
    houses: int              # completed listings behind the median
    median_days: float
    average_days: float
    fastest: int
    slowest: int
    watching: int            # still advertised, still being timed


def _watched_batches(db: Session, region: str) -> list[int]:
    """Every for-sale batch a customer has actually seen.

    Staged and preview batches are left out deliberately. They hold a second
    copy of houses already counted from the live batch, and counting a house
    twice moves the median toward whichever week happens to be mid-upload.
    """
    return [b.id for b in db.query(ImportBatch.id).filter(
        ImportBatch.batch_type == BatchType.FOR_SALE.value,
        ImportBatch.region == region,
        ImportBatch.status.in_(("published", "archived")),
    ).all()]


def _identity(slug_id, address, suburb) -> str | None:
    """What makes two rows THE SAME HOUSE across weeks.

    A listing carried forward for a month is four rows and one house. Counted
    per row it is four sales, all with the same duration, and a quiet week reads
    as a spike purely because the book got older. Same rule the weekly
    taken-off count uses, for the same reason.
    """
    slug = str(slug_id or "").strip()
    return slug or address_key(address, suburb)


def observed_days_to_sell(db: Session, region: str = "Auckland", *,
                          suburb: str | None = None,
                          suburbs: list[str] | None = None,
                          ptype: str | None = None) -> Observed | None:
    """Time on market for houses we watched appear and then disappear.

    `suburbs` takes the caller's already-resolved spellings (the suburb
    dropdown is built from trimmed names while the stored values are not, so a
    single exact string misses rows); `suburb` is the plain single-name form
    for callers that have not resolved anything.

    Returns None — never a zero — when there is not enough yet.
    """
    batch_ids = _watched_batches(db, region)
    if not batch_ids:
        return None

    want = [s for s in (suburbs or ([suburb] if suburb else [])) if s]

    # Matched through canonical_type(), not the raw string. The source portal
    # serves a Chinese-NZ audience and emits types in Chinese — 独立屋, 独立别墅
    # and 独立式住宅 are all houses — so a filter comparing raw strings would
    # quietly answer out of a fraction of the listings.
    kind = ptype.strip().lower() if isinstance(ptype, str) and ptype.strip() else None

    def _base():
        q = db.query(PropertyForSale.slug_id, PropertyForSale.address,
                     PropertyForSale.suburb, PropertyForSale.first_seen_at,
                     PropertyForSale.link_dead_at,
                     PropertyForSale.property_type).filter(
            PropertyForSale.import_batch_id.in_(batch_ids),
            PropertyForSale.first_seen_at.isnot(None))
        if want:
            q = q.filter(PropertyForSale.suburb.in_(want))
        return q

    def _wanted(pt) -> bool:
        return kind is None or canonical_type(pt).lower() == kind

    # Completed: the advertisement is gone, so the clock stopped.
    done: dict[str, float] = {}
    for slug, address, sub, seen, dead, pt in _base().filter(
            PropertyForSale.link_dead_at.isnot(None)).all():
        key = _identity(slug, address, sub)
        seen, dead = as_utc(seen), as_utc(dead)
        if not key or seen is None or dead is None or not _wanted(pt):
            continue
        days = (dead - seen).total_seconds() / 86400.0
        if days < MIN_DAYS or days > MAX_DAYS:
            continue
        # The same house can carry a departure date in more than one batch. The
        # earliest one is when it actually went; a later copy is bookkeeping.
        if key not in done or days < done[key]:
            done[key] = days

    if len(done) < MIN_HOUSES:
        return None

    # Still advertised. Not part of the median — a house that has not sold has
    # no time-to-sell yet, and folding today's date in would drag the figure
    # toward however long the slowest unsold listings have been sitting. It is
    # reported alongside so the sample size can be read for what it is.
    watching = {
        k for k in (
            _identity(slug, address, sub)
            for slug, address, sub, _seen, _dead, pt in _base().filter(
                PropertyForSale.link_dead_at.is_(None)).all()
            if _wanted(pt))
        if k
    }

    vals = sorted(done.values())
    mid = len(vals) // 2
    median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return Observed(
        houses=len(vals),
        median_days=round(median, 1),
        average_days=round(sum(vals) / len(vals), 1),
        fastest=int(vals[0]),
        slowest=int(vals[-1]),
        watching=len(watching),
    )
