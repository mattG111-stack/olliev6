"""Sections a developer could approach, whether or not anyone is selling them.

    "if you're a Developer and you're in Grey Lynn and you miss out on a
     property you're looking for a over 1000 m2 zoned Terrace houses can we
     find them?"

That question is two stored attributes and a join, and it was unanswerable only
because the rows did not exist. The subdivision engine never asked whether a
house was for sale — it takes a zone, a land area, a title type and a section
rate — so once the parcel layer is loaded, every section in a suburb runs on
exactly the same rules as a listing.

TWO THINGS THIS REFUSES TO GUESS, both because the wrong answer is expensive:

    A parcel with no zone is not assessed. The zone is the engine's first gate
    — a Single House section is never subdividable whatever its size — so a
    missing zone is a missing answer, not a permissive one. They are counted
    and reported instead, because "we have no zoning for 400 of the 900 parcels
    you searched" is a fact the searcher needs and a blank screen is not.

    A parcel under a special-character overlay is excluded by default. In the
    inner-west suburbs this decides most of the answer: a site can be zoned for
    terraces and carry a villa that cannot legally come down. A list that
    ignored it would be mostly untouchable sites, and the developer would know
    that before the end of the first page.

AND ONE THING IT WILL NOT SAY. There is no purchase price for a house nobody
has listed, so no profit is returned — only the yield. "This section takes five
terraces" is a fact about the land. "This section makes $900,000" would need a
price nobody has been quoted.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from models import BatchType, ImportBatch, LandParcel, PropertyForSale, User
from pricing import subdivision as SD
from pricing import zones as Z
from security import require_active
from site_layout import terrace_layout

router = APIRouter(prefix="/api/offmarket", tags=["offmarket"])

# Enough to be worth a developer's time to read. Not a page of a thousand.
MAX_ROWS = 200


class Site(BaseModel):
    """One section, with the yield and whether its shape backs it up."""
    parcel_id: str
    address: str | None = None
    suburb: str | None = None
    appellation: str | None = None
    area_m2: float | None = None
    zone: str | None = None
    title_type: str | None = None
    # What the engine makes of it, on the same rules a listing gets.
    strategy: str | None = None
    dwellings: int | None = None
    # And what the boundary makes of the engine. Null when there is no yield
    # to test; below the yield when the shape is the binding constraint.
    fits: int | None = None
    shape_limited: bool = False
    # Listed right now, so it is not an off-market lead — returned anyway, and
    # flagged, because a developer who just missed out wants to know which one
    # they missed.
    is_listed: bool = False
    # Metres to the nearest main of each kind. A DISTANCE, not a right to
    # connect: capacity, depth and the operator's approval decide that, and none
    # of them are in any published layer. Null means not known — never "nothing
    # nearby", which is a different answer and would be acted on differently.
    stormwater_m: float | None = None
    wastewater_m: float | None = None
    water_m: float | None = None
    ring: list[list[float]] = []


class Search(BaseModel):
    """The results, and every reason a parcel did not make them.

    A count that does not reconcile is a count nobody can act on. An empty
    screen and a suburb with no zoning loaded look identical, and they need
    opposite responses.
    """
    sites: list[Site] = []
    searched: int = 0
    no_zone: int = 0
    special_character: int = 0
    too_small: int = 0
    not_subdividable: int = 0
    note: str = ""


def _listed_keys(db: Session, region: str) -> set[str]:
    """Addresses currently advertised, so a lead can be told from a listing."""
    b = (db.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                 ImportBatch.region == region,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    if b is None:
        return set()
    from addresses import address_key
    out = set()
    for a, s in db.query(PropertyForSale.address, PropertyForSale.suburb).filter(
            PropertyForSale.import_batch_id == b.id).all():
        k = address_key(a, s)
        if k:
            out.add(k)
    return out


@router.get("/search", response_model=Search)
def search(
    region: str = "Auckland",
    suburb: str | None = Query(None, max_length=120),
    min_area_m2: float = Query(0, ge=0, le=1_000_000),
    zone: str | None = Query(None, max_length=120),
    # Metres to the nearest wastewater main. The filter a developer reaches for
    # second, after size: a big well-zoned flat parcel with no wastewater within
    # reach is not a site, it is a bill.
    #
    # A parcel with NO figure is kept, not dropped. Null means we have not
    # computed it or no network is loaded, and silently excluding those would
    # hide most of the region the first time somebody used the filter — while
    # looking like the region simply has no sites.
    max_wastewater_m: float | None = Query(None, ge=0, le=2000),
    include_listed: bool = False,
    include_special_character: bool = False,
    limit: int = Query(100, ge=1, le=MAX_ROWS),
    db: Session = Depends(get_db),
    user: User = Depends(require_active),
) -> Search:
    """Every section matching the filters, assessed like a listing.

    The Grey Lynn question is `suburb=Grey Lynn`, `min_area_m2=1000`,
    `zone=Residential - Terrace Housing and Apartment Building Zone`.
    """
    q = db.query(LandParcel).filter(LandParcel.region == region)
    if suburb:
        q = q.filter(LandParcel.suburb.ilike(f"%{suburb.strip()}%"))
    if min_area_m2:
        q = q.filter(LandParcel.area_m2 >= min_area_m2)
    if zone:
        q = q.filter(LandParcel.zone == zone)
    # isinstance, not "is not None". Called straight as a function — which is
    # what the tests do, and what any other router would do to reuse it — an
    # omitted argument is FastAPI's Query object, not None. It is not None, so
    # the obvious check passes, and it reaches SQLite as a bind parameter that
    # fails inside the driver with a message naming neither this filter nor this
    # endpoint. Same trap already documented on suburb_stats.
    if isinstance(max_wastewater_m, (int, float)):
        # Keeps the unknowns. See the parameter's note: dropping them would
        # empty the region the first time the filter was used, and look like
        # the region having no sites.
        q = q.filter((LandParcel.wastewater_m <= max_wastewater_m)
                     | (LandParcel.wastewater_m.is_(None)))
    rows = q.order_by(LandParcel.area_m2.desc()).limit(4000).all()

    if not rows:
        return Search(note=_nothing_note(db, region, suburb, min_area_m2, zone))

    listed = _listed_keys(db, region)
    from addresses import address_key

    out = Search(searched=len(rows))
    for p in rows:
        if p.zone is None:
            out.no_zone += 1
            continue
        if p.special_character and not include_special_character:
            out.special_character += 1
            continue

        sd = SD.compute(
            zone=p.zone,
            land_area=p.area_m2,
            # No listing means no purchase price. The engine already handles
            # this: it returns the yield and withholds the profit, which is the
            # honest answer rather than a limitation.
            buy_price=None,
            section_rate=None,
            title_type=p.title_type,
            address=p.address,
        )
        if not sd.can_subdivide:
            if sd.section_value_method in ("thab_site_too_small", "thab_too_small"):
                out.too_small += 1
            else:
                out.not_subdividable += 1
            continue

        ring = json.loads(p.ring) if p.ring else []
        fits = None
        limited = False
        if sd.dwellings and ring:
            laid = terrace_layout(ring, int(sd.dwellings))
            fits, limited = laid.fits, laid.shape_limited

        is_listed = bool(address_key(p.address, p.suburb) in listed) if p.address else False
        if is_listed and not include_listed:
            continue

        out.sites.append(Site(
            parcel_id=p.linz_id, address=p.address, suburb=p.suburb,
            appellation=p.appellation, area_m2=p.area_m2, zone=p.zone,
            title_type=p.title_type, strategy=sd.best_strategy,
            dwellings=sd.dwellings, fits=fits, shape_limited=limited,
            is_listed=is_listed,
            stormwater_m=p.stormwater_m, wastewater_m=p.wastewater_m,
            water_m=p.water_m, ring=ring,
        ))
        if len(out.sites) >= limit:
            break

    if not out.sites:
        out.note = _empty_note(out)
    return out


def _empty_note(s: Search) -> str:
    """Which filter emptied it. Same rule the assistant follows for comps: a
    dead end with nothing to do about it is not an answer."""
    if s.no_zone and s.no_zone >= s.searched * 0.5:
        return (f"{s.no_zone:,} of the {s.searched:,} sections here have no "
                f"zoning loaded, so they could not be assessed at all. Load the "
                f"council zone layer for this region and run it again.")
    if s.special_character:
        return (f"{s.special_character:,} matched on size and zone and sit under "
                f"a special-character overlay, so the existing house cannot "
                f"simply come down. Include them to see the list.")
    if s.too_small:
        return (f"{s.too_small:,} matched the zone and are below the site floor "
                f"for a development worth doing.")
    return (f"{s.searched:,} sections matched the size and zone, and none of "
            f"them are subdividable — most likely title: only freehold gives an "
            f"owner land they can divide.")


def _nothing_note(db: Session, region: str, suburb, min_area, zone) -> str:
    """Nothing matched the filters — which is a different thing from nothing
    being loaded, and the two need opposite responses."""
    total = db.query(LandParcel).filter(LandParcel.region == region).count()
    if not total:
        return (f"No parcels are loaded for {region}. The boundary layer has to "
                f"be imported before an area can be searched.")
    if suburb:
        here = (db.query(LandParcel)
                .filter(LandParcel.region == region,
                        LandParcel.suburb.ilike(f"%{suburb.strip()}%")).count())
        if not here:
            return (f"No sections are loaded under a suburb matching "
                    f"{suburb!r}, out of {total:,} in {region}.")
        wider = (db.query(LandParcel)
                 .filter(LandParcel.region == region,
                         LandParcel.suburb.ilike(f"%{suburb.strip()}%"),
                         LandParcel.area_m2 >= (min_area or 0)).count())
        if zone and wider:
            return (f"{wider:,} sections in {suburb} are {min_area:,.0f} m² or "
                    f"larger, and none of them carry that zone. Try without the "
                    f"zone filter to see what they are.")
        return (f"{here:,} sections are loaded in {suburb} and none reach "
                f"{min_area:,.0f} m². The largest is the place to start.")
    return f"Nothing matched, out of {total:,} sections loaded for {region}."
