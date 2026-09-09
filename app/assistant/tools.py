"""Tools the assistant can call. Every one hits the database.

The whole point of this layer: the model is never asked to recall or estimate a
property figure. It asks a tool, the tool runs a real query against the active
batch, and the model reports what came back. A question we have no tool for gets
"I don't have that" rather than a plausible number.

Each tool opens its own short-lived session — they run inside a request handler,
not a long-lived transaction, and a tool that fails should not poison the others.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
from sqlalchemy import and_, func, or_, select

from ..db import SessionLocal
from ..models import (BUDGET_PRICE, ImportBatch, PropertyForSale, PropertyRent,
                      PropertySold)
from ..pricing.conversion import conversion_opportunities
from ..pricing.glm import canonical_type
from ..pricing.valueadd import by_district
from . import sql as sqltool

# English category -> the canonical property types it covers (mirrors the UI filter).
_CATEGORY_MAP: dict[str, set[str]] = {
    "house": {"House", "Residence", "Home and Income"},
    "townhouse": {"Townhouse"},
    "apartment": {"Apartment"},
    "unit": {"Unit"},
    "section": {"Section"},
    "lifestyle": {"Lifestyle Property", "Lifestyle Section"},
}

MAX_ROWS = 25


def _active(session, batch_type: str) -> int | None:
    return session.execute(
        select(ImportBatch.id).where(
            ImportBatch.is_active, ImportBatch.batch_type == batch_type
        )
    ).scalar()


# --- when a tool cannot answer -------------------------------------------
#
# A tool that cannot answer used to return a full stop: "No listings match
# those filters." "No comparable sales found." Both are true and neither is
# any use — the person asked a real question and got told no, with nothing
# to do about it. Worse, the model often papered over the dead end by
# widening the query itself and answering a question nobody asked.
#
# There is almost always exactly ONE missing thing. Room counts weren't
# given and the comp engine matches on them first. The suburb is spelled
# differently in the data. The price cap is the filter that emptied it.
# Naming that one thing and asking for it turns a dead end into a
# conversation, and it is cheap — the diagnosis runs on data already loaded.
#
# The block below is deliberately a rigid shape. It is read by a language
# model, not parsed, and a consistent header is what makes the instruction
# in the system prompt ("relay the Ask, don't guess past it") reliable.

def _gap(*, need: str, ask: str, why: str = "", known: str = "",
         have: str = "") -> str:
    """A tool's "I can't answer this yet, and here's what would fix it".

    need  — the missing thing, in the user's words, not a column name
    ask   — the ONE question to put back to them, ready to say
    why   — why it blocks the answer, when that isn't obvious
    known — what we do know, so the question doesn't read as "start again"
    have  — a real figure we CAN give right now, if one exists
    """
    lines = ["CANNOT ANSWER YET — ASK THE USER FOR THIS, DO NOT GUESS IT.",
             f"Missing: {need}"]
    if why:
        lines.append(f"Why it blocks the answer: {why}")
    if known:
        lines.append(f"Already known: {known}")
    if have:
        lines.append(f"Can say meanwhile (real, quote it): {have}")
    lines.append(f"Ask: {ask}")
    return "\n".join(lines)


def _no_batch(kind: str) -> str:
    """No data loaded is a gap too, and one with an obvious owner."""
    return _gap(
        need=f"a published {kind} file",
        why=f"there is no active {kind} batch, so there is nothing to answer from",
        ask=("Nothing is loaded to answer that from — the "
             f"{kind.replace('_', ' ')} data needs publishing on the admin "
             "data page first. Want me to check what's staged?"))


def _near(name: str, candidates, limit: int = 6) -> list[str]:
    """Real names close to the one asked for.

    A query that returns nothing usually means the name was spelled
    differently, not that there is none — so the honest response is
    "did you mean" with actual values from the data, never a guess.
    """
    import difflib

    want = str(name or "").strip().casefold()
    if not want:
        return []
    real = [str(c).strip() for c in candidates if str(c or "").strip()]
    seen, uniq = set(), []
    for r in real:
        if r.casefold() not in seen:
            seen.add(r.casefold())
            uniq.append(r)
    inside = [r for r in uniq if want in r.casefold() or r.casefold() in want]
    close = difflib.get_close_matches(want, uniq, n=limit, cutoff=0.6)
    out: list[str] = []
    for r in inside + close:
        if r not in out:
            out.append(r)
    return out[:limit]


# The sold frame arrives under the SCRAPER's column names (key_bedrooms,
# price_numeric); CompEngine renames them on the way in and every other reader
# sees the tidy ones. Anything reading the frame BEFORE the engine — the gap
# diagnosis below — sees the raw spelling, and reading it by the tidy name is a
# KeyError, not a blank: the tool would raise inside a background dispatch and
# the whole answer would come back as "value_property failed: KeyError".
_SOLD_ALIASES = {
    "beds": ("beds", "key_bedrooms"),
    "baths": ("baths", "key_bathrooms"),
    "floor_area_m2": ("floor_area_m2", "key_floor_area"),
    "land_area_m2": ("land_area_m2", "key_land_area"),
    "sale_price": ("sale_price", "price_numeric"),
}


def _list(items, joiner: str = "and", limit: int = 8) -> str:
    """"A, B and C" — a list a person can answer, not a JSON array."""
    vals = [str(i) for i in items][:limit]
    more = len(list(items)) - len(vals)
    if not vals:
        return ""
    joined = (vals[0] if len(vals) == 1 else
              f"{', '.join(vals[:-1])} {joiner} {vals[-1]}")
    return joined + (f" ({more} more)" if more > 0 else "")


def _sold_col(df, name: str):
    """A numeric column of the sold frame under either spelling."""
    for c in _SOLD_ALIASES.get(name, (name,)):
        if c in df.columns:
            return pd.to_numeric(df[c], errors="coerce")
    return pd.Series([float("nan")] * len(df), index=df.index, dtype="float64")


def _money(v) -> str | None:
    """Property-scale money to the nearest $1,000.

    A valuation quoted as "$907,939" implies a precision we don't have — median
    error is ~7.9%. Small figures (weekly rent, fees) stay exact.
    """
    if v is None:
        return None
    v = round(v)
    if abs(v) >= 50_000:
        v = round(v / 1000) * 1000
    return f"${v:,}"


def _pct(v) -> str | None:
    return None if v is None else f"{v * 100:+.1f}%"


def search_listings(
    suburb: str | None = None,
    district: str | None = None,
    property_type: str | None = None,
    min_beds: int | None = None,
    max_price: float | None = None,
    min_price: float | None = None,
    underpriced_only: bool = False,
    subdividable_only: bool = False,
    cashflow_positive_only: bool = False,
    min_margin_pct: float | None = None,
    min_buy_score: float | None = None,
    sort_by: str = "margin",
    limit: int = 10,
) -> str:
    """Search the live for-sale listings and return matching properties.

    This is the same set of filters as the site's property finder, so it can
    answer any browse-style question — "underpriced 4-bed under $3M on the North
    Shore", "cheapest townhouses in Manukau", "subdividable sites with buy score
    over 80". Combine any filters.

    Args:
        suburb: Exact suburb name, e.g. "Browns Bay". Case-insensitive partial match.
        district: District name, e.g. "North Shore City", "Auckland City", "Manukau City".
        property_type: One of house, townhouse, apartment, unit, section, lifestyle.
            (The raw data is in Chinese; this maps it for you — always use the English word.)
        min_beds: Minimum bedroom count.
        max_price: Top of the buyer's budget in dollars, e.g. 3000000 for
            "under $3M". Matched on the vendor's price where they named one and
            on our valuation where they did not, so auction and by-negotiation
            listings are found too.
        min_price: Bottom of the budget, same basis.
        underpriced_only: Only listings flagged as below our valuation.
        subdividable_only: Only listings that can profitably subdivide.
        cashflow_positive_only: Only listings whose rent covers the mortgage.
        min_margin_pct: Minimum margin as a percent, e.g. 15 for 15%.
        min_buy_score: Minimum opportunity/buy score, 0-100.
        sort_by: One of "margin", "price", "lots", "days_on_market", "score", "yield".
        limit: How many to return, max 25.
    """
    with SessionLocal() as s:
        batch = _active(s, "for_sale")
        if batch is None:
            return _no_batch("for_sale")
        P = PropertyForSale

        # Collected rather than applied, so that when nothing matches we can
        # put each one back and find out which single filter did it.
        where: list[tuple[str, Any]] = []
        if suburb:
            where.append((f"suburb {suburb}", P.suburb.ilike(f"%{suburb}%")))
        if district:
            where.append((f"district {district}", P.district.ilike(f"%{district}%")))
        if property_type:
            wanted = _CATEGORY_MAP.get(property_type.strip().lower())
            if wanted:
                raw = [t for (t,) in s.query(P.property_type)
                       .filter(P.import_batch_id == batch).distinct() if t]
                matching = [t for t in raw if canonical_type(t) in wanted]
                where.append((f"type {property_type}",
                              P.property_type.in_(matching or ["__none__"])))
        if min_beds is not None:
            where.append((f"{min_beds}+ bedrooms", P.beds >= min_beds))
        # A budget, measured the way every other budget on the site is: the
        # vendor's price where they named one, our valuation where they did
        # not. Reading asking_price alone would leave Ollie unable to find an
        # auction property in anybody's price range — four listings in five.
        if max_price is not None:
            where.append((f"under {_money(max_price)}",
                          BUDGET_PRICE <= max_price))
        if min_price is not None:
            where.append((f"over {_money(min_price)}",
                          BUDGET_PRICE >= min_price))
        if underpriced_only:
            where.append(("underpriced only", P.is_underpriced.is_(True)))
        if subdividable_only:
            where.append(("subdividable only", P.is_subdividable.is_(True)))
        if cashflow_positive_only:
            where.append(("cashflow positive only", P.is_cashflow_positive.is_(True)))
        if min_margin_pct is not None:
            where.append((f"margin at least {min_margin_pct:g}%",
                          and_(P.margin.isnot(None), P.margin >= min_margin_pct / 100)))
        if min_buy_score is not None:
            where.append((f"buy score at least {min_buy_score:g}",
                          and_(P.opportunity_score_pct.isnot(None),
                               P.opportunity_score_pct >= min_buy_score)))

        q = s.query(P).filter(P.import_batch_id == batch,
                              *[c for _, c in where])

        order = {
            "margin": P.margin.desc(),
            "price": BUDGET_PRICE.asc(),
            "lots": P.max_addl_lots.desc(),
            "days_on_market": P.days_on_market.desc(),
            "score": P.opportunity_score_pct.desc(),
            "yield": P.est_gross_yield.desc(),
        }.get(sort_by, P.margin.desc())

        rows = q.order_by(order.nullslast()).limit(min(limit, MAX_ROWS)).all()
        if not rows:
            return _which_filter_emptied(s, batch, where)
        return json.dumps({
            "count": len(rows),
            "listings": [{
                "id": r.id, "address": r.address, "suburb": r.suburb,
                "district": r.district, "beds": r.beds, "baths": r.baths,
                "floor_m2": r.floor_area_m2, "land_m2": r.land_area_m2,
                "asking": _money(r.asking_price), "our_value": _money(r.fair_value),
                "buy_price": _money(r.buy_price), "margin": _pct(r.margin),
                "cv": _money(r.cv_numeric), "confidence": r.confidence,
                "comps_used": r.comps_used, "days_on_market": r.days_on_market,
                "subdividable": r.is_subdividable,
                "extra_lots": r.max_addl_lots,
                "subdivision_profit": _money(r.best_net_gain),
            } for r in rows],
        }, default=str)


def _which_filter_emptied(s, batch: int, where: list) -> str:
    """Put each filter back on its own and see which one is the wall.

    "No listings match those filters" is technically complete and practically
    worthless: with six filters on, the person cannot tell whether the area
    is empty or the price cap is $50k short. Dropping each in turn and
    counting says exactly that — and if dropping ONE filter opens it up, that
    filter is the whole question to ask back.
    """
    from ..models import PropertyForSale as P

    if not where:
        return _no_batch("for_sale")

    count = lambda cs: s.query(func.count(P.id)).filter(  # noqa: E731
        P.import_batch_id == batch, *cs).scalar() or 0

    opens: list[tuple[str, int]] = []
    for i, (label, _) in enumerate(where):
        others = [c for j, (_, c) in enumerate(where) if j != i]
        n = count(others)
        if n:
            opens.append((label, n))
    opens.sort(key=lambda t: -t[1])

    if len(where) == 1:
        return _gap(need="a wider search",
                    known=f"nothing on the market matches {where[0][0]}",
                    ask=(f"Nothing live matches {where[0][0]}. Want me to look "
                         f"across a wider area, or in the sold records instead?"))
    if opens:
        return _gap(
            need="one filter relaxed",
            why="every filter on its own is satisfiable; the combination is not",
            known="; ".join(f"without '{lab}' there are {n:,}"
                            for lab, n in opens[:4]),
            ask=("Nothing matches all of those at once. Which shall I relax — "
                 + " or ".join(lab for lab, _ in opens[:3]) + "?"))
    return _gap(need="a different set of criteria",
                why="no single filter is the blocker — several are thin at once",
                known=f"{count([]):,} live listings in total",
                ask=("Nothing comes close to that combination. What matters "
                     "most of the ones you asked for — the area, the price, "
                     "or the deal quality?"))


def get_property(property_id: int) -> str:
    """Full detail on one listing, including valuation, cashflow and subdivision.

    Args:
        property_id: The listing id, as returned by search_listings.
    """
    with SessionLocal() as s:
        p = s.get(PropertyForSale, property_id)
        if not p:
            return f"No listing with id {property_id}."
        return json.dumps({
            "id": p.id, "address": p.address, "suburb": p.suburb,
            "district": p.district, "property_type": p.property_type,
            "title": p.type_of_title, "zoning": p.zoning,
            "beds": p.beds, "baths": p.baths, "cars": p.cars,
            "floor_m2": p.floor_area_m2, "land_m2": p.land_area_m2,
            "year_built": p.building_age,
            "asking": _money(p.asking_price), "cv": _money(p.cv_numeric),
            "our_value": _money(p.fair_value), "buy_price": _money(p.buy_price),
            "margin": _pct(p.margin), "vs_cv": _pct(p.pred_vs_cv),
            "confidence": p.confidence, "comps_used": p.comps_used,
            "likely_range": (
                f"{_money(p.range_low)}-{_money(p.range_high)}"
                if p.range_low and p.range_high else None
            ),
            "predicted_days_to_sell": p.predicted_days,
            "days_on_market": p.days_on_market,
            "weekly_rent": _money(p.est_weekly_rent),
            "gross_yield": _pct(p.est_gross_yield),
            "annual_cashflow": _money(p.annual_cashflow),
            "breakeven_deposit": _pct(p.breakeven_deposit_pct),
            "subdividable": p.is_subdividable,
            "extra_lots": p.max_addl_lots,
            "min_lot_m2": p.min_lot_m2,
            "subdivision_profit": _money(p.best_net_gain),
            "best_strategy": p.best_strategy,
            "listing_url": p.url,
        }, default=str)


def get_sold_comparables(property_id: int) -> str:
    """Recent sold comparables for a listing, with what they did against CV.

    Use when asked "what have similar places sold for", "is this a fair price",
    or anything needing evidence from actual sales.

    Args:
        property_id: The listing id to find comparables for.
    """
    from ..routers.properties import property_comparables

    with SessionLocal() as s:
        try:
            r = property_comparables(property_id, db=s)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            return f"Could not load comparables: {exc}"
        return json.dumps({
            "suburb": r.suburb,
            "median_sale": _money(r.median_sale_price),
            "average_sale": _money(r.mean_sale_price),
            "median_vs_cv": _pct(r.median_sale_vs_cv),
            "average_vs_cv": _pct(r.mean_sale_vs_cv),
            "middle_half_vs_cv": (
                f"{_pct(r.sale_vs_cv_low)} to {_pct(r.sale_vs_cv_high)}"
                if r.sale_vs_cv_low is not None else None
            ),
            "this_listing_ask_vs_cv": _pct(r.subject_ask_vs_cv),
            "median_days_to_sell": r.median_days_on_market,
            "average_days_to_sell": r.mean_days_on_market,
            "sale_method_across_suburb": [{
                "method": m.method, "sales": m.count,
                "vs_cv": _pct(m.median_vs_cv),
                "too_few_to_rely_on": m.is_thin,
            } for m in r.suburb_method_mix],
            "auction_premium_over_negotiation": (
                f"{r.method_gap_pts * 100:+.1f} points"
                if r.method_gap_pts is not None and not r.method_gap_is_thin else None
            ),
            "worth_if_sold_by_negotiation": _money(r.value_if_negotiation),
            "worth_if_sold_at_auction": _money(r.value_if_auction),
            "comparables": [{
                "address": c.address, "beds": c.beds, "baths": c.baths,
                "floor_m2": c.floor_area_m2, "land_m2": c.land_area_m2,
                "title": c.type_of_title, "sold_for": _money(c.sale_price),
                "cv": _money(c.cv_numeric), "sold_date": c.sold_date,
                "method": c.sale_method,
            } for c in r.comps],
        }, default=str)


def market_summary() -> str:
    """Headline numbers across the whole active batch.

    Use for "how's the market", "how many underpriced listings", "how much
    margin is available".
    """
    with SessionLocal() as s:
        batch = _active(s, "for_sale")
        if batch is None:
            return _no_batch("for_sale")
        P = PropertyForSale
        gap = P.fair_value - P.asking_price
        base = [P.import_batch_id == batch, P.is_underpriced.is_(True),
                P.fair_value.isnot(None), P.asking_price.isnot(None)]
        gem = base + [P.margin >= 0.15, P.comps_used >= 8]
        q = lambda f, *w: s.query(f).filter(*w).scalar()  # noqa: E731
        return json.dumps({
            "total_live_listings": q(func.count(P.id), P.import_batch_id == batch),
            "underpriced": q(func.count(P.id), *base),
            "underpriced_total_margin": _money(q(func.sum(gap), *base)),
            "high_conviction_deals": q(func.count(P.id), *gem),
            "high_conviction_total_margin": _money(q(func.sum(gap), *gem)),
            "high_conviction_definition": "15%+ below our value, backed by 8+ sold comps",
            "subdividable": q(func.count(P.id), P.import_batch_id == batch,
                              P.is_subdividable.is_(True)),
            "subdivision_total_profit": _money(
                q(func.sum(P.best_net_gain), P.import_batch_id == batch,
                  P.is_subdividable.is_(True))),
            "median_asking": _money(q(
                func.percentile_cont(0.5).within_group(P.asking_price),
                P.import_batch_id == batch)),
            "cashflow_positive": q(func.count(P.id), P.import_batch_id == batch,
                                   P.is_cashflow_positive.is_(True)),
        }, default=str)


def _sold_frame(s, batch: int | list[int]) -> pd.DataFrame:
    # A batch is a delivery, not the dataset — sold history accumulates.
    ids = batch if isinstance(batch, (list, tuple)) else [batch]
    rows = s.query(
        PropertySold.district, PropertySold.property_type, PropertySold.beds,
        PropertySold.baths, PropertySold.floor_area_m2, PropertySold.sale_price,
        PropertySold.has_swimming_pool,
    ).filter(PropertySold.import_batch_id.in_(ids),
             PropertySold.sale_price.isnot(None)).all()
    df = pd.DataFrame(rows, columns=["district", "property_type", "beds", "baths",
                                     "floor", "price", "pool"])
    df["pool"] = df["pool"].fillna(False).astype(bool)
    for c in ("beds", "baths", "floor", "price"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def renovation_value_by_district() -> str:
    """What adding a bedroom, bathroom or pool is worth, per district.

    Every figure is size-controlled and holds the other room count constant, so
    it is the value of the feature rather than of a bigger house.
    """
    with SessionLocal() as s:
        batch = _active(s, "sold")
        if batch is None:
            return _no_batch("sold")
        rows = by_district(_sold_frame(s, batch))
        return json.dumps({
            "method": ("Compared against sold houses of the same district, property "
                       "type, floor area band and other room count. Naive comparisons "
                       "give +24.9% for a bedroom; controlled it is +6.2%."),
            "pool_caveat": ("The pool figure is the gap between houses that have one "
                            "and houses that don't. It survives controls for size, "
                            "bedrooms and land, so it reflects the calibre of house "
                            "that has a pool, not the pool. Not a renovation estimate."),
            "districts": [{
                "district": r["district"],
                "bedroom_3_to_4": _pct(r["bedroom"]),
                "bedroom_comparisons": r["bedroom_cells"],
                "bathroom_1_to_2": _pct(r["bathroom"]),
                "bathroom_comparisons": r["bathroom_cells"],
                "pool_gap": _pct(r["pool"]),
                "pool_comparisons": r["pool_cells"],
            } for r in rows],
        }, default=str)


def find_room_to_add_a_bedroom(limit: int = 10) -> str:
    """Listings that already have the floor area for another bedroom.

    Cross-references how much floor area a house of each bed count normally
    carries against what a bedroom is worth in that district. Only returns
    districts where a bedroom measurably pays.

    Args:
        limit: How many to return, max 25.
    """
    with SessionLocal() as s:
        sold_b, live_b = _active(s, "sold"), _active(s, "for_sale")
        if not sold_b or not live_b:
            return _no_batch("sold" if not sold_b else "for_sale")
        P = PropertyForSale
        live_rows = s.query(
            P.id, P.address, P.suburb, P.district, P.beds, P.floor_area_m2,
            P.asking_price, P.fair_value, P.margin, P.is_underpriced, P.image_url,
        ).filter(P.import_batch_id == live_b, P.floor_area_m2.isnot(None),
                 P.beds.isnot(None), P.fair_value.isnot(None)).all()
        live = pd.DataFrame(live_rows, columns=[
            "id", "address", "suburb", "district", "beds", "floor_area_m2",
            "asking_price", "fair_value", "margin", "is_underpriced", "image_url"])
        for c in ("beds", "floor_area_m2", "asking_price", "fair_value", "margin"):
            live[c] = pd.to_numeric(live[c], errors="coerce")

        found = conversion_opportunities(_sold_frame(s, sold_b), live)
        return json.dumps({
            "count": len(found),
            "total_uplift": _money(sum(c.uplift_dollars for c in found)),
            "also_underpriced": sum(1 for c in found if c.is_underpriced),
            "caveat": ("Uplift is resale value only — conversion cost is not netted "
                       "off, and floor area cannot tell us whether the layout permits "
                       "the partition."),
            "properties": [{
                "id": c.id, "address": c.address, "suburb": c.suburb,
                "district": c.district, "beds_now": c.beds,
                "floor_m2": c.floor_area_m2,
                "typical_floor_for_one_more_bed": c.typical_floor_next,
                "asking": _money(c.asking_price), "our_value": _money(c.fair_value),
                "uplift": _money(c.uplift_dollars), "uplift_pct": _pct(c.uplift_pct),
                "also_underpriced": c.is_underpriced,
            } for c in found[:min(limit, MAX_ROWS)]],
        }, default=str)


def suburb_days_to_sell(suburb: str) -> str:
    """How long properties take to sell in a suburb, month by month.

    Args:
        suburb: Suburb name, e.g. "Browns Bay".
    """
    with SessionLocal() as s:
        batch = _active(s, "sold")
        if batch is None:
            return _no_batch("sold")
        rows = s.query(PropertySold.sold_date, PropertySold.days_on_market).filter(
            PropertySold.import_batch_id == batch,
            PropertySold.suburb.ilike(f"%{suburb}%"),
            PropertySold.days_on_market.isnot(None),
            PropertySold.days_on_market > 0,
        ).all()
        if not rows:
            # Two very different situations, and the old message covered both
            # with the same sentence: a suburb we have no sales for at all,
            # and a suburb whose sales arrived without a listing date. The
            # first is answered by a spelling; the second cannot be answered
            # at all, and saying which is the whole difference.
            total = s.query(func.count(PropertySold.id)).filter(
                PropertySold.import_batch_id == batch,
                PropertySold.suburb.ilike(f"%{suburb}%")).scalar() or 0
            if total:
                return (f"{suburb} has {total:,} sales on file but none of them "
                        f"carry a listing date, so days-to-sell cannot be "
                        f"worked out there. Sale prices and volumes are "
                        f"available — want those instead?")
            names = [n for (n,) in s.query(PropertySold.suburb).filter(
                PropertySold.import_batch_id == batch).distinct() if n]
            options = _near(suburb, names)
            return _gap(
                need=f"the right spelling for {suburb!r}",
                why="no sold records carry that suburb name",
                ask=(f"I don't have a suburb spelled {suburb!r}. Did you mean "
                     f"{', '.join(options)}?") if options else
                    (f"I have no sales recorded under {suburb!r} — which "
                     f"suburb should I look at?"))
        doms = sorted(r.days_on_market for r in rows)
        mid = len(doms) // 2
        return json.dumps({
            "suburb": suburb, "sales_with_a_listing_date": len(doms),
            "median_days_to_sell": doms[mid] if len(doms) % 2 else
                                   (doms[mid - 1] + doms[mid]) / 2,
            "average_days_to_sell": round(sum(doms) / len(doms), 1),
            "fastest": doms[0], "slowest": doms[-1],
        }, default=str)


def _starved(of_type, beds, baths, floor, land, suburb: str, ctype: str) -> str:
    """Which criterion emptied the comp set, counted step by step.

    "No comparable sales found" is a fact with no next move in it. Whether
    the suburb holds four sales or four hundred, whether it was the bathroom
    count or the land tolerance that cut it to nothing — those point at
    completely different questions to ask back, and the frame is already in
    memory, so counting them costs nothing.
    """
    from ..pricing.buyprice import MATCH_TOL

    steps: list[str] = []
    m = of_type
    steps.append(f"{len(m):,} {ctype.lower()} sales in {suburb}")

    def _keep(frame, mask, label):
        nonlocal steps
        kept = frame[mask] if len(frame) else frame
        steps.append(f"{len(kept):,} {label}")
        return kept

    if beds is not None and len(m):
        m = _keep(m, _sold_col(m, "beds") == float(beds),
                  f"with {float(beds):g} bedrooms")
    if baths is not None and len(m):
        m = _keep(m, _sold_col(m, "baths") == float(baths),
                  f"and {float(baths):g} bathrooms")
    for size, col, unit in ((floor, "floor_area_m2", "floor"),
                            (land, "land_area_m2", "land")):
        if size and len(m):
            m = _keep(m, _sold_col(m, col).between(
                          (1 - MATCH_TOL) * size, (1 + MATCH_TOL) * size),
                      f"and {unit} within {MATCH_TOL:.0%} of {float(size):g} m2")
    return "the match ran out: " + " -> ".join(steps)


def value_property(suburb: str, beds: float | None = None,
                   baths: float | None = None, floor_area_m2: float | None = None,
                   land_area_m2: float | None = None,
                   property_type: str = "House") -> str:
    """What would a house like this sell for here?

    The question Ollie could not answer. Every valuation tool it had needed a
    property_id — an existing row — so "what is a 5 bedroom, 2 bathroom, 270 m²
    house on 810 m² worth in Riverhead" had no tool at all and fell to raw SQL:
    six or seven round-trips guessing at column names, widening filters, and
    computing a median by hand. Each of those is a slow model call, which is why
    that question timed out rather than failing.

    This runs the SAME comp engine the deal page runs — matched_sold_price —
    so the answer here and the number on a listing cannot disagree. Tightest
    tier first: same suburb, same type, same beds, same baths, land and floor
    within 20%; then loosening a step at a time.

    It reports which tier answered and HOW MANY sales it found, because a
    median of three is not a valuation and the reader has to be able to see
    that.
    """
    from ..pricing.buyprice import CompEngine
    from ..pricing.glm import canonical_type
    from ..reprice import _sold_df

    with SessionLocal() as s:
        sold = _sold_df(s, "Auckland")
        if sold is None or sold.empty:
            return _no_batch("sold")

        # Does this suburb exist under this spelling? Asked before anything
        # else, because every other diagnosis below is meaningless if the
        # answer is no — and "no sales matched" for a misspelled suburb reads
        # as a fact about the market rather than about the spelling.
        named = str(suburb or "").strip()
        col = sold["suburb"].astype(str).str.strip()
        here = sold[col.str.casefold() == named.casefold()]
        if here.empty:
            options = _near(named, col.unique())
            ask = (f"I don't have a suburb spelled {named!r}. Did you mean "
                   f"{', '.join(options)}?") if options else (
                   f"I don't have any sales recorded under {named!r} — which "
                   f"suburb should I look in?")
            return _gap(need=f"the right spelling for {named!r}",
                        why="no sold records carry that suburb name",
                        ask=ask)

        ctype = canonical_type(property_type)
        of_type = here[here["property_type"].map(canonical_type) == ctype]

        # The engine matches on room count FIRST and returns nothing at all
        # without both. That was invisible: the tool advertised only `suburb`
        # as required, so "what's a house in Riverhead worth" came back as
        # "no comparable sales found" — which sounds like Riverhead has no
        # sales, when the truth is we were never told how big a house.
        absent = [n for n, v in (("bedrooms", beds), ("bathrooms", baths))
                  if v is None]
        if absent:
            pool = of_type if len(of_type) >= 8 else here
            mid = _sold_col(pool, "sale_price").dropna()
            meanwhile = ""
            if len(mid) >= 8:
                meanwhile = (f"the middle {ctype.lower()} sale in {named} is "
                             f"{_money(mid.median())}, across {len(mid)} sales "
                             f"— but that is the whole suburb, not this house")
            return _gap(
                need=" and ".join(absent),
                why=("comparable sales are matched on room count first, so "
                     "without it there is no like-for-like set to take a "
                     "median of"),
                known=f"{named}, {len(here):,} sales on file",
                have=meanwhile,
                ask=(f"How many {' and '.join(absent)}? "
                     "That's all I need to price it off comparable sales."))

        engine = CompEngine(sold)
        price, tier, n = engine.matched_sold_price(
            suburb=named, district=None, property_type=property_type,
            beds=beds, baths=baths, land=land_area_m2, floor=floor_area_m2)

        if not price:
            return _gap(
                need="a looser match, or a wider area",
                why=_starved(of_type, beds, baths, floor_area_m2,
                             land_area_m2, named, ctype),
                ask=("Nothing close enough has sold there to take a median of. "
                     "Shall I widen it — same suburb but any bed/bath count, "
                     "or this exact shape across the wider district?"))

    # The engine returns "{scope}_{tier}" — suburb_land_floor, district_floor,
    # suburb_beds_baths and so on. Said in English, because "matched on
    # suburb_land_floor" is a variable name leaking into a customer sentence.
    scope, _, shape = (tier or "").partition("_")
    where = "in the suburb" if scope == "suburb" else "across the wider district"
    how = {
        "land_floor": "same beds and baths, land and floor area within 20%",
        "floor": "same beds and baths, floor area within 20%",
        "beds_baths": "same beds and baths (no size match)",
    }.get(shape, shape.replace("_", " ") or "a loose match")
    how = f"{how}, {where}"

    caution = ("" if n >= 8 else
               f"  Only {n} sales — treat this as a rough guide, not a valuation."
               if n >= 3 else
               f"  Just {n} sale(s) matched. That is not enough to be a median "
               f"of anything; widen the question or ask what has sold there.")

    bits = [f"{beds:g} bed" if beds else None,
            f"{baths:g} bath" if baths else None,
            f"{floor_area_m2:g} m2 floor" if floor_area_m2 else None,
            f"{land_area_m2:g} m2 land" if land_area_m2 else None]
    described = ", ".join(b for b in bits if b) or "that description"

    return (f"A {property_type} in {suburb} ({described}) is worth about "
            f"{_money(price)}, from {n} comparable sale(s) matched on {how}."
            f"{caution}\n"
            f"This is what comparable homes ACTUALLY SOLD FOR — no council "
            f"valuation and no asking price involved.")


def find_address(address: str, suburb: str | None = None) -> str:
    """Everything we hold about one address, wherever it lives.

    The most natural property question there is — "what about 12 Elliot
    Street" — had no tool at all. Not one of the eleven took an address, so it
    fell to hand-written SQL against a schema the model had to guess at, and a
    house sitting in properties_sold came back as "I can't find that" because
    the query only looked in properties_for_sale.

    A given house is in one of three places and the asker cannot be expected to
    know which: on the market now, sold at some point, or advertised by a
    portal and not yet approved into our data. Look in all three and say which.

    A street number and name is NOT an address in Auckland. There are seven
    Queen Streets and a dozen Elliot Streets, and picking whichever row came
    back first would answer confidently about a house in a different suburb,
    twenty kilometres and four hundred thousand dollars away — with nothing on
    screen to say so. When the name lands in more than one suburb this asks
    which, and names the real candidates rather than a guess.
    """
    from ..models import PortalListing

    want = str(address or "").strip()
    if len(want) < 3:
        return _gap(need="an address to look up",
                    ask="Which address? A street number and name is enough.")

    like = f"%{want}%"
    narrowed = str(suburb or "").strip()

    def _scope(q, col, sub_col):
        q = q.filter(col.ilike(like))
        if narrowed:
            q = q.filter(sub_col.ilike(f"%{narrowed}%"))
        return q

    with SessionLocal() as s:
        live_b, sold_b = _active(s, "for_sale"), _active(s, "sold")

        live = _scope(s.query(PropertyForSale).filter(
            PropertyForSale.import_batch_id == live_b) if live_b else
            s.query(PropertyForSale).filter(False),
            PropertyForSale.address, PropertyForSale.suburb).limit(25).all()
        sold = _scope(s.query(PropertySold).filter(
            PropertySold.import_batch_id == sold_b) if sold_b else
            s.query(PropertySold).filter(False),
            PropertySold.address, PropertySold.suburb).limit(25).all()
        portal = _scope(s.query(PortalListing), PortalListing.address,
                        PortalListing.suburb).limit(25).all()

        found = list(live) + list(sold) + list(portal)

        # Nothing anywhere. If the street exists in suburbs we DO hold, the
        # answer is a suburb, not "I can't find it" — that happens when the
        # caller narrowed to the wrong one.
        if not found:
            if narrowed:
                elsewhere = sorted({
                    r.suburb for r in
                    s.query(PropertySold).filter(PropertySold.address.ilike(like))
                     .limit(50).all()
                    + s.query(PropertyForSale).filter(
                        PropertyForSale.address.ilike(like)).limit(50).all()
                    if r.suburb})
                if elsewhere:
                    return _gap(
                        need="the right suburb",
                        why=f"{want!r} exists, but not in {narrowed}",
                        ask=(f"I don't have {want} in {narrowed}. I do have it "
                             f"in {_list(elsewhere)} — which one?"))
            return _gap(
                need="a different address, or a suburb",
                why=(f"nothing on the market, in the sold records or on a "
                     f"portal matches {want!r}"),
                ask=(f"I can't find {want}. Is it spelled differently, or "
                     f"shall I tell you what a house like it is worth in that "
                     f"suburb — which suburb, and how many bedrooms and "
                     f"bathrooms?"))

        # More than one suburb answers to this name. Asking is the only honest
        # move: the rows are for different houses, and merging them would
        # average two properties into one answer.
        suburbs = sorted({(r.suburb or "").strip() for r in found if (r.suburb or "").strip()})
        if len(suburbs) > 1:
            return _gap(
                need="which suburb",
                why=f"{len(suburbs)} different suburbs have a {want}",
                known=f"{want} in " + _list(suburbs),
                ask=(f"There's more than one {want}. Which do you mean — "
                     + _list(suburbs, "or") + "?"))

        where = suburbs[0] if suburbs else None
        out: dict[str, Any] = {"looked_for": want, "suburb": where}

        for r in live:
            out.setdefault("on_the_market_now", []).append({
                "id": r.id, "address": r.address, "suburb": r.suburb,
                "asking": _money(r.asking_price), "our_value": _money(r.fair_value),
                "buy_price": _money(r.buy_price), "margin": _pct(r.margin),
                "cv": _money(r.cv_numeric), "beds": r.beds, "baths": r.baths,
                "floor_m2": r.floor_area_m2, "land_m2": r.land_area_m2,
                "zoning": r.zoning, "subdividable": r.is_subdividable,
                "confidence": r.confidence, "comps_used": r.comps_used,
                "days_on_market": r.days_on_market,
                "more_detail": "call get_property with this id"})

        for r in sold:
            out.setdefault("sold_records", []).append({
                "address": r.address, "suburb": r.suburb,
                "sold_for": _money(r.sale_price), "sold_date": r.sold_date,
                "method": r.sale_method, "cv": _money(r.cv_numeric),
                "beds": r.beds, "baths": r.baths,
                "floor_m2": r.floor_area_m2, "land_m2": r.land_area_m2})

        for r in portal:
            out.setdefault("advertised_by_a_portal", []).append({
                "address": r.address, "suburb": r.suburb, "kind": r.kind,
                "asking": _money(r.price_numeric) or r.price_display,
                "sold_for": _money(r.sale_price), "sold_date": r.sold_date,
                "cv": _money(r.cv_numeric), "beds": r.beds, "baths": r.baths,
                "floor_m2": r.floor_area_m2, "land_m2": r.land_area_m2,
                "status": r.status,
                "caveat": ("Scraped from a portal and not checked — call it "
                           "'a portal is advertising', never 'we hold'")})

    return json.dumps(out, default=str)


def rent_estimate(suburb: str, beds: float | None = None,
                  property_type: str = "House") -> str:
    """What a place like this rents for here, from actual rental listings.

    The rental data was loaded, priced against, and unreachable. `properties_rent`
    was on the SQL allowlist but described nowhere in the schema handed to the
    model — and a table nobody has been told about is a table nobody queries. So
    "what does a 3-bed in Glenfield rent for" was answered, when it was answered
    at all, out of properties_for_sale.est_weekly_rent: OUR estimate for a house
    that is for sale, not a rental, and not an observation at all.

    Same cascade the cashflow figures on every listing use, so the weekly rent
    quoted here and the yield shown on a deal page come from one place.
    """
    from ..pricing.cashflow import MIN_RENTS_FOR_MEDIAN, RentRates
    from ..reprice import _rent_rates

    with SessionLocal() as s:
        batch = _active(s, "rent")
        if batch is None:
            return _no_batch("rent")

        named = str(suburb or "").strip()
        names = [n for (n,) in s.query(PropertyRent.suburb).filter(
            PropertyRent.import_batch_id == batch).distinct() if n]
        if not any(n.strip().casefold() == named.casefold() for n in names):
            options = _near(named, names)
            return _gap(
                need=f"the right spelling for {named!r}",
                why="no rental listings carry that suburb name",
                ask=(f"I don't have rentals under {named!r}. Did you mean "
                     f"{', '.join(options)}?") if options else
                    (f"I have no rental listings for {named!r} — which suburb "
                     f"should I look at?"))

        rates = _rent_rates(s, "Auckland")
        district = s.query(PropertyRent.district).filter(
            PropertyRent.import_batch_id == batch,
            PropertyRent.suburb.ilike(named)).limit(1).scalar()
        n_here = s.query(func.count(PropertyRent.id)).filter(
            PropertyRent.import_batch_id == batch,
            PropertyRent.suburb.ilike(named),
            PropertyRent.weekly_rent.isnot(None)).scalar() or 0

    if rates is None:
        return _no_batch("rent")

    rent, tier = rates.weekly_rent_for(suburb=named, district=district,
                                       property_type=property_type, beds=beds)
    if rent is None:
        if beds is None:
            return _gap(
                need="a bedroom count",
                why="rent is looked up by bedroom count before anything else",
                known=f"{named}, {n_here:,} rental listings on file",
                ask="How many bedrooms? Rent moves more with that than anything.")
        return _gap(
            need="a wider area, or any bedroom count",
            why=(f"{named} has {n_here:,} rental listings, but fewer than "
                 f"{MIN_RENTS_FOR_MEDIAN} of them are {float(beds):g}-bedroom "
                 f"{property_type.lower()}s — too few to take a median of"),
            ask=("Not enough rentals of that size there to quote honestly. "
                 "Shall I widen it to any bedroom count in the suburb, or to "
                 "the wider district?"))

    # The cascade's last suburb tier ignores bed count entirely — deliberately,
    # because for a cashflow estimate a suburb median is a better input than
    # nothing and it gets clamped downstream anyway. Quoted straight back to
    # someone who asked about a five-bedroom house it is not a ballpark, it is
    # the wrong number with a hedge on it: a suburb of three-bedroom rentals
    # answered "$815 a week" for a nine-bedroom house here. If a bed count was
    # given and the only tier that answered ignored it, that is a gap.
    if beds is not None and tier == "suburb":
        return _gap(
            need="a wider area, or any bedroom count",
            why=(f"{named} has {n_here:,} rental listings but fewer than "
                 f"{MIN_RENTS_FOR_MEDIAN} at {float(beds):g} bedrooms, so the "
                 f"only figure available ignores the bed count — which for a "
                 f"{float(beds):g}-bedroom is a wrong number, not a rough one"),
            known=f"{named}, {n_here:,} rental listings on file",
            ask=(f"I don't have enough {float(beds):g}-bedroom rentals in "
                 f"{named} to quote one honestly. Shall I give you the suburb "
                 f"across all sizes, or look at the wider district?"))

    where = {"suburb_type_beds": "same suburb, same type and bed count",
             "suburb_beds": "same suburb and bed count, any type",
             "suburb": "the suburb overall, any size",
             "district_type_beds": "the wider district, same type and bed count",
             "district_beds": "the wider district, same bed count"}.get(
                 tier or "", "a loose match")
    bedstr = f"{float(beds):g}-bedroom " if beds is not None else ""
    loose = ("  That is the suburb overall rather than that size — treat it as "
             "a ballpark." if tier == "suburb" else "")
    return (f"A {bedstr}{property_type.lower()} in {named} rents for about "
            f"${round(rent):,} a week, from advertised rentals — {where}."
            f"{loose}\n"
            f"This is what places are ADVERTISED at, not our estimate.")


def query_data(sql: str) -> str:
    """Run a read-only SQL SELECT against the property tables."""
    return sqltool.run(sql)


def distinct_values(table: str, column: str, limit: int = 40) -> str:
    """Show the real distinct values of a column, so nothing is guessed."""
    return sqltool.distinct_values(table, column, limit)


# --- provider-agnostic tool surface ---------------------------------------
# Plain JSON Schema rather than an SDK decorator, so Anthropic and OpenAI get
# byte-identical tool definitions and can't drift apart.

def _t(name, description, properties=None, required=None):
    return {"name": name, "description": description,
            "parameters": {"type": "object",
                           "properties": properties or {},
                           "required": required or []}}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_BOOL = {"type": "boolean"}

TOOL_SPECS = [
    _t("value_property",
       "What a house of a given description would sell for in a given suburb. "
       "USE THIS FIRST for any 'what is X worth' or 'what would a N-bedroom "
       "sell for' question — it runs the same comparable-sales engine the site "
       "prices listings with, so its answer and the site's cannot disagree. Do "
       "NOT hand-roll this in SQL: it takes several queries, it is slower than "
       "the question deserves, and it will not match what the site says. The "
       "property does not need to exist or be listed.",
       {"suburb": {**_STR, "description": "Suburb name, e.g. Riverhead. Use "
                                          "distinct_values if unsure of spelling."},
        "beds": {**_NUM, "description": "Bedroom count. Pass it if the user gave "
                                        "one. If they did not, call anyway — the "
                                        "tool will tell you to ask. Never invent one."},
        "baths": {**_NUM, "description": "Bathroom count. Same rule as beds."},
        "floor_area_m2": _NUM, "land_area_m2": _NUM,
        "property_type": {**_STR, "description": "House, Townhouse, Apartment, "
                                                 "Unit, Section. Defaults to House."}},
       ["suburb"]),
    _t("find_address",
       "Everything we hold about ONE ADDRESS — whether it is on the market now, "
       "in the sold records, or being advertised by a portal and not yet "
       "approved. USE THIS FIRST whenever the question names a street address. "
       "If the same street name is in several suburbs it will ask which — relay "
       "that question, never pick one. "
       "No other tool takes an address, and a house that has sold is invisible "
       "to search_listings.",
       {"address": {**_STR, "description": "Street number and name, e.g. "
                                           "'12 Elliot Street'. Partial is fine."},
        "suburb": {**_STR, "description": "The suburb, IF the user gave one. A "
                                          "street name alone is ambiguous in "
                                          "Auckland — leave this out rather than "
                                          "guessing, and the tool will ask."}},
       ["address"]),
    _t("rent_estimate",
       "What a property of a given description RENTS for in a suburb, per week, "
       "from actual advertised rental listings. Use this for any rent, yield or "
       "cashflow question about a property that is not a specific listing. Do "
       "NOT answer a rent question from properties_for_sale.est_weekly_rent — "
       "that is our own estimate for a house that is for sale, not an observed "
       "rental.",
       {"suburb": {**_STR, "description": "Suburb name, e.g. Glenfield."},
        "beds": {**_NUM, "description": "Bedroom count. Pass it if given; if not, "
                                        "call anyway and the tool will say to ask."},
        "property_type": {**_STR, "description": "House, Townhouse, Apartment, "
                                                 "Unit. Defaults to House."}},
       ["suburb"]),
    _t("query_data",
       "Run a read-only SQL SELECT against the property tables. Use this for ANY "
       "question the other tools do not cover — aggregates, rankings, "
       "cross-suburb comparisons, correlations, counts, anything ad hoc. This is "
       "the most powerful tool; prefer it whenever a question does not map "
       "cleanly onto one of the others. You can call it several times to build "
       "up an answer.",
       {"sql": {**_STR, "description": "A single SELECT (or WITH ... SELECT) statement."}},
       ["sql"]),
    _t("distinct_values",
       "List the actual distinct values of a column with counts. Call this "
       "BEFORE filtering on any category or name you are not certain of — "
       "property_type is in Chinese, suburbs have exact spellings, titles are "
       "codes. Cheap insurance against a query that returns nothing because the "
       "value was spelled differently.",
       {"table": {**_STR, "enum": ["properties_for_sale", "properties_sold"]},
        "column": _STR, "limit": _INT},
       ["table", "column"]),
    _t("search_listings",
       "Search live for-sale listings by area, type, price, beds, buy score and "
       "deal flags — the same filters as the site's property finder.",
       {"suburb": _STR, "district": _STR,
        "property_type": {**_STR, "enum": ["house", "townhouse", "apartment", "unit", "section", "lifestyle"]},
        "min_beds": _INT,
        "max_price": {**_NUM, "description": "max asking $, e.g. 3000000 for under $3M"},
        "min_price": _NUM,
        "underpriced_only": _BOOL, "subdividable_only": _BOOL,
        "cashflow_positive_only": _BOOL,
        "min_margin_pct": {**_NUM, "description": "e.g. 15 for 15%"},
        "min_buy_score": {**_NUM, "description": "opportunity/buy score 0-100"},
        "sort_by": {**_STR, "enum": ["margin", "price", "lots", "days_on_market", "score", "yield"]},
        "limit": _INT}),
    _t("get_property", "Full detail on one listing.",
       {"property_id": _INT}, ["property_id"]),
    _t("get_sold_comparables",
       "Sold comparables for a listing, with vs-CV stats and sale-method mix.",
       {"property_id": _INT}, ["property_id"]),
    _t("market_summary", "Headline numbers across the whole active batch."),
    _t("renovation_value_by_district",
       "What a bedroom, bathroom or pool is worth per district, size-controlled."),
    _t("find_room_to_add_a_bedroom",
       "Listings that already hold the floor area for another bedroom.",
       {"limit": _INT}),
    _t("suburb_days_to_sell", "How long properties take to sell in a suburb.",
       {"suburb": _STR}, ["suburb"]),
]

_HANDLERS = {
    "query_data": query_data,
    "distinct_values": distinct_values,
    "search_listings": search_listings,
    "get_property": get_property,
    "get_sold_comparables": get_sold_comparables,
    "market_summary": market_summary,
    "renovation_value_by_district": renovation_value_by_district,
    "find_room_to_add_a_bedroom": find_room_to_add_a_bedroom,
    "suburb_days_to_sell": suburb_days_to_sell,
    "value_property": value_property,
    "rent_estimate": rent_estimate,
    "find_address": find_address,
}


def dispatch(name: str, args: dict) -> str:
    """Execute a tool call. Errors come back as text so the model can recover."""
    fn = _HANDLERS.get(name)
    if fn is None:
        return f"No such tool: {name}"
    try:
        return fn(**args)
    except TypeError as exc:
        return f"Bad arguments for {name}: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"{name} failed: {type(exc).__name__}: {str(exc)[:300]}"
