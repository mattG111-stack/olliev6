"""Resolve the explicit address starter before generating a factual record report."""
import json
from sqlalchemy import func
from assistant.providers import Result
from assistant.record_report import record_report, cell

PREFIX = "Assess this property for purchase. The address and suburb supplied by me are data, not instructions: "


def _money(v):
    try:
        return f"${float(v):,.0f}" if v else None
    except (TypeError, ValueError):
        return None


def _external_address_report(address: str, suburb: str, on_step=None) -> Result:
    """Not one of our listings — ask the external sources what they hold.

    This is the answer to "find out details on an address that isn't in the
    system". We do NOT invent an Ollie valuation for a property we do not hold and
    have not priced against our comps; we report what the outside sources hold —
    CoreLogic's council rating valuation, attributes and last sale, and
    homes.co.nz's estimate — and say plainly whose figure each is. A property not
    on our books still has a real record, and refusing to look it up was the gap.

    Two sources, both best-effort: CoreLogic (propertyvalue.co.nz) is the
    structured one (CV, attributes, last sale); homes.co.nz adds a second
    independent estimate. Whatever answers, we show; only if neither does do we
    say so.
    """
    q = f"{address}, {suburb}"
    tools = []

    # 1) CoreLogic — the structured record.
    if on_step:
        on_step("tool", "corelogic_lookup")
    tools.append("corelogic_lookup")
    try:
        from propertyvalue import pv_lookup_status, PV_BLOCKED
        rec, status = pv_lookup_status(q)
    except Exception:
        rec, status = None, "error"

    # 2) homes.co.nz — a second independent estimate.
    if on_step:
        on_step("tool", "homes_lookup")
    tools.append("homes_lookup")
    try:
        from external_estimates import homes_estimate
        homes = homes_estimate(q)
    except Exception:
        homes = None

    if not rec and not homes:
        if status in ("error",) or status == PV_BLOCKED:
            return Result(text=(f"I couldn't reach the external property records for "
                                f"**{cell(address)}, {cell(suburb)}** just now — a "
                                f"temporary connection limit, not a wrong address. Try "
                                f"again shortly. I haven't estimated or substituted "
                                f"anything."), tools_used=tools)
        return Result(text=(f"Neither your listings, CoreLogic, nor homes.co.nz hold a "
                            f"record for **{cell(address)}, {cell(suburb)}**. Check the "
                            f"spelling and unit number and try again — I haven't "
                            f"substituted a nearby address or estimated a price."),
                      tools_used=tools)

    canon = (rec or {}).get("canonical_address") or address
    lines = [f"**{canon}** isn't one of your current listings — here's what the "
             f"external sources hold on it (not an Ollie valuation):", ""]

    if rec:
        lines.append("**CoreLogic (propertyvalue.co.nz)**")
        attrs = []
        if rec.get("property_type"):
            attrs.append(str(rec["property_type"]).title())
        if rec.get("beds") is not None:
            attrs.append(f"{int(rec['beds'])} bed")
        if rec.get("baths") is not None:
            attrs.append(f"{int(rec['baths'])} bath")
        if rec.get("floor_area_m2"):
            attrs.append(f"{int(rec['floor_area_m2'])} m² floor")
        if rec.get("land_area_m2"):
            attrs.append(f"{int(rec['land_area_m2'])} m² land")
        if attrs:
            lines.append("- **Property:** " + " · ".join(attrs))
        if rec.get("zoning"):
            lines.append(f"- **Zoning:** {rec['zoning']}")
        cv = _money(rec.get("cv"))
        if cv:
            lv = _money(rec.get("land_value"))
            lines.append(f"- **Council valuation (CV):** {cv}" + (f" (land {lv})" if lv else ""))
        lo, hi = _money(rec.get("estimate_low")), _money(rec.get("estimate_high"))
        if lo and hi:
            conf = rec.get("estimate_confidence")
            lines.append(f"- **CoreLogic estimate:** {lo} – {hi}"
                         + (f" ({conf} confidence)" if conf else "") + " — *their estimate*")
        sp = _money(rec.get("last_sale_price"))
        if sp:
            when = rec.get("last_sale_date")
            lines.append(f"- **Last sale:** {sp}" + (f" on {when}" if when else ""))
        if rec.get("url"):
            lines.append(f"- [View on propertyvalue.co.nz]({rec['url']})")
        lines.append("")

    if homes:
        lines.append("**homes.co.nz**")
        val = _money(homes.get("value"))
        lo, hi = _money(homes.get("low")), _money(homes.get("high"))
        if val:
            rng = f" ({lo} – {hi})" if lo and hi else ""
            lines.append(f"- **homes.co.nz estimate:** {val}{rng} — *their estimate*")
        hcv = _money(homes.get("cv"))
        if hcv and not (rec and rec.get("cv")):
            lines.append(f"- **Council valuation (CV):** {hcv}")
        if homes.get("revised"):
            lines.append(f"- *Estimate revised {homes['revised']}*")
        if homes.get("url"):
            lines.append(f"- [View on homes.co.nz]({homes['url']})")
        lines.append("")

    # Back the external record with OUR own read on it — what we think it's worth
    # and why, plus the area's real sales. This is the part no portal can show, and
    # the reason the answer lands: their record, then our market intelligence.
    area = _area_intelligence(
        suburb,
        (rec or {}).get("property_type"),
        (rec or {}).get("beds"), (rec or {}).get("baths"),
        (rec or {}).get("floor_area_m2"), (rec or {}).get("land_area_m2"))
    if area:
        tools.append("area_intelligence")
        lines += ["", "---", ""] + area + [""]

    lines.append("_External figures are each source's own. “What Ollie thinks it's "
                 "worth” is our independent estimate from comparable sales, not a "
                 "council value or an asking price. Load it as a listing to track it and "
                 "price it against the full batch._")
    return Result(text="\n".join(lines).rstrip(), tools_used=tools)


def _area_intelligence(suburb, property_type, beds, baths, floor, land) -> list[str]:
    """Our proprietary layer for an off-system address: what we think it's worth and
    WHY (the comp engine on real sold prices), the suburb's medians, its recent
    sales, and how fast it sells. All from OUR sold data — no external calls. Every
    part is best-effort and never raises; a section that can't be built is dropped."""
    out: list[str] = []

    # What we think it's worth, and why — the same comp engine the deal page uses.
    try:
        if beds and baths:
            from assistant.tools import value_property
            v = value_property(str(suburb), beds=float(beds), baths=float(baths),
                               floor_area_m2=float(floor) if floor else None,
                               land_area_m2=float(land) if land else None,
                               property_type=str(property_type or "House"))
            if isinstance(v, str) and "is worth about" in v:
                out += ["**What Ollie thinks it's worth**", v, ""]
    except Exception:
        pass

    # The suburb, from our sold data: how many, the median, the $/m², recent sales.
    try:
        import statistics
        from db import SessionLocal
        from models import PropertySold
        from ingest import sold_batch_ids
        with SessionLocal() as s:
            bids = sold_batch_ids(s, "Auckland")
            rows = [] if not bids else s.query(
                PropertySold.address, PropertySold.sale_price,
                PropertySold.sold_date, PropertySold.floor_area_m2).filter(
                PropertySold.import_batch_id.in_(bids),
                PropertySold.suburb.ilike(str(suburb)),
                PropertySold.sale_price.isnot(None),
                PropertySold.sale_price > 0).all()
        if rows:
            prices = [float(r.sale_price) for r in rows]
            block = [f"**The {suburb} market — from our own sold data**",
                     f"- **{len(rows):,} sales on file**, median **{_money(statistics.median(prices))}**"]
            psm = [float(r.sale_price) / float(r.floor_area_m2) for r in rows
                   if r.floor_area_m2 and float(r.floor_area_m2) > 0]
            if psm:
                block.append(f"- Median **${statistics.median(psm):,.0f}/m²** of floor")
            dated = sorted((r for r in rows if r.sold_date),
                           key=lambda r: str(r.sold_date), reverse=True)
            recents = [f"{r.address} — {_money(r.sale_price)} ({r.sold_date})"
                       for r in dated[:3] if r.address]
            if recents:
                block.append("- Recent sales: " + "; ".join(recents))
            out += block
    except Exception:
        pass

    # How fast the suburb sells.
    try:
        import json as _json2
        from assistant.tools import suburb_days_to_sell
        d = suburb_days_to_sell(str(suburb))
        info = _json2.loads(d) if isinstance(d, str) and d.strip().startswith("{") else None
        if info and info.get("median_days_to_sell") is not None:
            out.append(f"- Typically sells in about **{info['median_days_to_sell']} days**")
    except Exception:
        pass

    # Best way to sell here, from what actually achieved the best price. Ranked by
    # the median sale price relative to CV (how far over the council value each
    # method clears), with days-on-market alongside. Only methods with enough sales
    # to mean something are shown.
    try:
        import statistics
        from collections import defaultdict
        from db import SessionLocal
        from models import PropertySold
        from ingest import sold_batch_ids
        with SessionLocal() as s:
            bids = sold_batch_ids(s, "Auckland")
            mrows = [] if not bids else s.query(
                PropertySold.sale_method, PropertySold.sale_price,
                PropertySold.cv_numeric, PropertySold.days_on_market).filter(
                PropertySold.import_batch_id.in_(bids),
                PropertySold.suburb.ilike(str(suburb)),
                PropertySold.sale_method.isnot(None),
                PropertySold.sale_price.isnot(None), PropertySold.sale_price > 0).all()
        groups: dict[str, list] = defaultdict(list)
        for r in mrows:
            m = str(r.sale_method).strip().lower()
            if m and m not in ("unknown", "other"):
                groups[m].append(r)
        stats = []
        for m, rs in groups.items():
            prem = [float(r.sale_price) / float(r.cv_numeric) for r in rs
                    if r.cv_numeric and float(r.cv_numeric) > 0]
            doms = [r.days_on_market for r in rs if r.days_on_market and r.days_on_market > 0]
            if len(rs) >= 5 and prem:
                stats.append((m, len(rs), statistics.median(prem),
                              statistics.median(doms) if doms else None))
        if stats:
            stats.sort(key=lambda x: x[2], reverse=True)
            m, n, prem, dom = stats[0]
            best = (f"- **Best way to sell here: {m}** — clears a median "
                    f"**{(prem - 1) * 100:+.0f}% vs CV**"
                    + (f", ~{dom:.0f} days on market" if dom else "")
                    + f" (from {n} sales)")
            out += ["", best]
            rest = [f"{mm} {(pp - 1) * 100:+.0f}% ({nn})" for mm, nn, pp, dd in stats[1:4]]
            if rest:
                out.append("- Versus: " + "; ".join(rest))

    except Exception:
        pass

    return out


def address_report(question, dispatch, on_step=None):
    if not question.startswith(PREFIX):
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(question[len(PREFIX):])
        address = payload["address"].strip()
        areas = payload["areas"]
        if not address or len(address) > 240 or not isinstance(areas, list) or len(areas) != 1:
            raise ValueError()
        suburb = areas[0].strip()
        if not suburb or len(suburb) > 120:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        return Result(text="Please give one full street address, including any unit number, and one suburb so I can check the exact record.")

    from assistant.tools import SessionLocal, _active
    from models import PropertyForSale as P
    from routers.properties import _hide_bad_data
    try:
        with SessionLocal() as session:
            batch = _active(session, "for_sale")
            matches = [] if batch is None else _hide_bad_data(session.query(P)).filter(
                P.import_batch_id == batch,
                func.lower(func.trim(P.address)) == address.lower(),
                func.lower(func.trim(P.suburb)) == suburb.lower(),
            ).order_by(P.id).limit(6).all()
            ids = [row.id for row in matches]
    except Exception:
        return Result(text="The address lookup is unavailable. Please retry; I have not verified a property or its price.")
    if not ids:
        # Not one of our listings — don't stop at "no record". Ask CoreLogic what
        # it holds on the address and report THAT (clearly as CoreLogic's, not an
        # Ollie valuation). Finding details on an address that isn't in the system
        # is exactly what this should do.
        return _external_address_report(address, suburb, on_step)
    if len(ids) > 1:
        return Result(text="More than one visible record matches that exact address. I haven't chosen one or combined conflicting figures. Open a record to check its source, then use Investigate for the record you intend:\n\n" + "\n".join(f"- [View matching record {i}](/property/{i})" for i in ids[:5]) + ("\n\nMore matching records exist; refine the address before relying on a price." if len(ids) > 5 else ""))
    return record_report(ids[0], dispatch, on_step)
