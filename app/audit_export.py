"""The live data as a workbook, with the checks already run.

    "also have an exel download for all live data so i can download it and you
     audit it for bugs"

There is already a CSV export, and a CSV of nine thousand rows is not an audit —
it is nine thousand rows. Every fault this system has shipped was visible in the
data at the time and nobody could see it, because seeing it meant knowing which
column to look down and what the number should have been:

  1,436 of 1,586 listings "advertising" a price, in a market where four houses
  in five sell by auction or negotiation and name no price at all
  a headline deal of +2,296% against a council valuation that had priced the
  land and not the house
  672 listings live where 9,281 had been the week before

So the first sheet is the checks, each one written as a question with the answer
next to it and a plain statement of what it should look like. A person scrolling
that sheet does not need to know this system to spot that something is wrong.
The second sheet is the data, so anything the checks did not think of can still
be found by hand.

WHAT COUNTS AS LIVE. Not "in the live batch" — a batch carries rows a customer
can never see: held back at review, advertisement gone, a placeholder price the
scraper invented, an unbelievable margin. Those rows are exactly where a fault
hides, so they are all here, with a column saying whether the row is visible and
one saying why not. Filter on it in Excel and you have either set.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import BatchType, ImportBatch, PropertyForSale
from .version import VERSION

# One row per column of the listings sheet: (heading, attribute).
#
# Explicit rather than "every column on the model" because an audit sheet whose
# columns move when somebody adds a field is one nobody can compare against last
# week's, and because the heading is doing work — "asking_price" and "the price
# the vendor named" are the same number to the database and not to a reader.
COLUMNS: list[tuple[str, str]] = [
    ("Visible on site", "_visible"),
    ("Why not visible", "_why_hidden"),
    ("ID", "id"),
    ("Listing ref", "slug_id"),
    ("Address", "address"),
    ("Suburb", "suburb"),
    ("District", "district"),
    ("Postcode", "postcode"),
    ("Property type", "property_type"),
    ("Title", "type_of_title"),
    ("Zoning", "zoning"),
    # --- what the listing says ---
    ("How it sells", "sale_method"),
    ("Classified as", "listing_type"),
    ("Asking price", "asking_price"),
    ("Where the asking came from", "asking_basis"),
    ("Beds", "beds"),
    ("Baths", "baths"),
    ("Floor m2", "floor_area_m2"),
    ("Land m2", "land_area_m2"),
    # --- council ---
    ("Council valuation", "cv_numeric"),
    ("Land value", "land_value_numeric"),
    ("Improvement value", "improvement_value_numeric"),
    # --- ours ---
    ("Our fair value", "fair_value"),
    ("Our buy price", "buy_price"),
    ("Margin %", "margin"),
    ("Market value", "market_value"),
    ("Predicted list", "predicted_list"),
    ("Comps used", "comps_used"),
    ("Confidence", "confidence"),
    ("Opportunity score", "opportunity_score"),
    ("Pricing path", "pricing_path"),
    # --- subdivision ---
    ("Can subdivide", "can_subdivide"),
    ("Min lot m2", "min_lot_m2"),
    ("Extra lots", "max_addl_lots"),
    ("Best strategy", "best_strategy"),
    ("Best net gain", "best_net_gain"),
    # --- cashflow ---
    ("Weekly rent", "est_weekly_rent"),
    ("Gross yield", "est_gross_yield"),
    ("Annual cashflow", "annual_cashflow"),
    # --- flags ---
    ("Underpriced", "is_underpriced"),
    ("Cashflow positive", "is_cashflow_positive"),
    ("Subdividable", "is_subdividable"),
    ("Held back", "is_held"),
    ("Hold reason", "hold_reason"),
    ("Not a deal because", "deal_block_reason"),
    # --- the advertisement ---
    ("Listing link", "url"),
    ("Photo", "image_url"),
    ("Photos held", "image_count"),
    ("Link last checked", "link_checked_at"),
    ("Link last answered", "link_last_result"),
    ("Times found gone", "link_gone_count"),
    ("Advertisement gone since", "link_dead_at"),
    ("Listed on", "listing_date"),
    ("Days on market", "days_on_market"),
    ("Agency", "agent1_company_name"),
]

_MONEY = {"asking_price", "cv_numeric", "land_value_numeric",
          "improvement_value_numeric", "fair_value", "buy_price",
          "market_value", "predicted_list", "best_net_gain",
          "annual_cashflow"}
_DATES = {"link_checked_at", "link_dead_at"}


def _visible_reason(p: PropertyForSale) -> str | None:
    """Why a row in the live batch is not on the site — mirrors _hide_bad_data.

    Deliberately the reason and not just a flag. "Hidden" tells an operator that
    something is wrong somewhere; "Held: below $10,000 margin" tells them what.
    """
    # Deferred: routers.properties imports this module for the endpoint, so a
    # top-level import here would be a cycle. By call time both are loaded.
    from .release import MARGIN_MAX_PCT
    from .routers.properties import _SECTION_TYPES

    if p.is_held:
        return f"Held at review: {p.hold_reason or 'no reason recorded'}"
    if p.link_dead_at is not None:
        return "Advertisement is gone (link check)"
    ask, cv = p.asking_price, p.cv_numeric
    last = p.valuation_last_sold_value
    if ask and ((cv and ask == cv) or (last and ask == last)):
        return "Asking price is the council valuation or the last sale, to the dollar"
    if p.margin is not None and p.margin > MARGIN_MAX_PCT:
        return f"Margin of {p.margin:.0%} is above the believable ceiling"
    if p.floor_area_m2 is None and (p.property_type or "") not in _SECTION_TYPES:
        return "A dwelling with no floor area — cannot be valued by size"
    return None


def _check_rows(rows: list, batch: ImportBatch | None) -> list[tuple]:
    """(question, answer, what it should look like, verdict).

    A check earns its place by having caught something, or by being the check
    that would have. Nothing here is a statistic for its own sake.
    """
    live = [p for p in rows if p._visible]
    n = len(live)
    out: list[tuple] = []

    def add(q, a, expect, bad: bool):
        out.append((q, a, expect, "LOOK AT THIS" if bad else "ok"))

    # --- the book itself ---
    add("Listings a customer can see", n,
        "The whole book. A sudden drop of thousands is a short upload, "
        "not a market that emptied.", n == 0)
    add("Rows in the batch but hidden", len(rows) - n,
        "Some is normal — held rows, dead links. Most of the batch is not.",
        len(rows) and (len(rows) - n) > 0.7 * len(rows))

    if not n:
        return out

    # --- the fault that shipped ---
    priced = [p for p in live if p.asking_price]
    add("Vendor actually named a price", f"{len(priced):,} of {n:,} "
        f"({len(priced) / n:.0%})",
        "Roughly one in five to two in five. Four Auckland houses in five sell "
        "by auction, tender or negotiation and name no price at all — so a high "
        "number here means a search price is being read as an asking price.",
        len(priced) / n > 0.60)

    kinds: dict[str, int] = {}
    for p in live:
        kinds[p.listing_type or "(blank)"] = kinds.get(p.listing_type or "(blank)", 0) + 1
    add("How they sell", ", ".join(f"{k}: {v:,}" for k, v in sorted(kinds.items())),
        "auction and negotiation should dominate. A batch that is nearly all "
        "'fixed' is a batch that has not read the sale method.",
        kinds.get("(blank)", 0) > 0.1 * n)

    # --- numbers that ran away ---
    silly = [p for p in live if p.fair_value and p.asking_price
             and p.fair_value > 2 * p.asking_price]
    add("Our value more than double the asking", len(silly),
        "Zero. Nobody lists a house at half its worth — this is a broken input, "
        "usually a council record that valued the land and not the house.",
        bool(silly))
    no_cv = [p for p in live if not p.cv_numeric]
    add("No council valuation", f"{len(no_cv):,} ({len(no_cv) / n:.0%})",
        "A fifth or so is normal for the raw feed.", len(no_cv) > 0.4 * n)
    no_fv = [p for p in live if not p.fair_value]
    add("No value of our own", f"{len(no_fv):,} ({len(no_fv) / n:.0%})",
        "Small. A listing we cannot value is a listing we cannot rank.",
        len(no_fv) > 0.3 * n)

    # --- the same house twice ---
    slugs: dict[str, int] = {}
    for p in live:
        if p.slug_id:
            slugs[p.slug_id] = slugs.get(p.slug_id, 0) + 1
    dupes = sum(v - 1 for v in slugs.values() if v > 1)
    add("The same listing more than once", dupes,
        "Zero. A duplicate is one house on the site twice, at two prices.",
        bool(dupes))
    blank_slug = [p for p in live if not p.slug_id]
    add("Listings with no reference", len(blank_slug),
        "Zero. Without one, next week's upload cannot tell this house from a "
        "new one, so it is carried for ever or duplicated.", bool(blank_slug))

    # --- the advertisement ---
    no_url = [p for p in live if not p.url]
    add("No link to the advertisement", len(no_url),
        "Zero. The link check cannot open what it has no address for, so these "
        "listings can never leave the site.", bool(no_url))
    no_pic = [p for p in live if not p.image_url]
    add("No photograph", f"{len(no_pic):,} ({len(no_pic) / n:.0%})",
        "Near zero.", len(no_pic) > 0.1 * n)

    never = [p for p in live if p.link_checked_at is None]
    add("Link never checked", f"{len(never):,} ({len(never) / n:.0%})",
        "Falls towards zero as the daily sweep gets round. Stuck high means the "
        "sweep is not reaching the whole book.", len(never) > 0.5 * n)
    now = datetime.now(timezone.utc)
    stale = [p for p in live if p.link_checked_at is not None
             and (now - _aware(p.link_checked_at)).days > 30]
    add("Link not checked in 30 days", len(stale),
        "Small. A listing nobody has opened in a month may have sold weeks ago.",
        len(stale) > 0.25 * n)

    # --- subdivision, the thing the product is for ---
    no_zone = [p for p in live if not p.zoning]
    add("No zoning", f"{len(no_zone):,} ({len(no_zone) / n:.0%})",
        "Matters more than it looks: no zoning means the site is never assessed "
        "for subdivision at all, so it silently cannot appear in that list.",
        len(no_zone) > 0.3 * n)
    add("Can be subdivided", len([p for p in live if p.can_subdivide]),
        "The whole point of the product. Zero means the assessment is not "
        "running.", not any(p.can_subdivide for p in live))

    # --- what was held, and why ---
    reasons: dict[str, int] = {}
    for p in rows:
        if p.is_held:
            reasons[p.hold_reason or "(none given)"] = \
                reasons.get(p.hold_reason or "(none given)", 0) + 1
    add("Held back at review",
        ", ".join(f"{k}: {v:,}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
        or "none",
        "Held rows are fine — they are the review working. A reason that "
        "suddenly dominates is a new fault, not a strict reviewer.", False)

    if batch is not None:
        out.append(("Batch", f"#{batch.id} · {batch.filename}",
                    f"{batch.rows_inserted or 0:,} rows loaded, "
                    f"{batch.rows_rejected or 0:,} rejected at the door", ""))
    return out


def _aware(d: datetime) -> datetime:
    """SQLite hands back a naive datetime; comparing it to an aware one raises."""
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def build(db: Session, region: str = "Auckland") -> bytes:
    """The workbook, as bytes."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    batch = (db.query(ImportBatch)
             .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                     ImportBatch.region == region,
                     ImportBatch.is_active.is_(True))
             .order_by(ImportBatch.id.desc()).first())
    rows: list[PropertyForSale] = []
    if batch is not None:
        rows = (db.query(PropertyForSale)
                .filter(PropertyForSale.import_batch_id == batch.id)
                .order_by(PropertyForSale.id.asc()).all())
    for p in rows:
        why = _visible_reason(p)
        p._why_hidden = why or ""
        p._visible = why is None

    wb = Workbook()
    head = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="14233A")
    bad_fill = PatternFill("solid", fgColor="FDE3DE")

    # ---- sheet 1: the checks ----
    ws = wb.active
    ws.title = "Checks"
    ws.append([f"Apex Property — live data check · build {VERSION} · "
               f"{datetime.now(timezone.utc):%d/%m/%Y}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(["What was asked", "What the data says",
               "What it should look like", ""])
    for c in ws[3]:
        c.font = head
        c.fill = head_fill
    for q, a, expect, verdict in _check_rows(rows, batch):
        ws.append([q, a, expect, verdict])
        if verdict == "LOOK AT THIS":
            for c in ws[ws.max_row]:
                c.fill = bad_fill
    for col, width in (("A", 38), ("B", 30), ("C", 78), ("D", 14)):
        ws.column_dimensions[col].width = width
    for r in ws.iter_rows(min_row=4):
        r[2].alignment = Alignment(wrap_text=True, vertical="top")
        r[0].alignment = Alignment(vertical="top")
    ws.freeze_panes = "A4"

    # ---- sheet 2: every row ----
    ws2 = wb.create_sheet("Listings")
    ws2.append([h for h, _ in COLUMNS])
    for c in ws2[1]:
        c.font = head
        c.fill = head_fill
    for p in rows:
        line = []
        for _, attr in COLUMNS:
            v = getattr(p, attr, None)
            if isinstance(v, datetime):
                v = _aware(v).strftime("%d/%m/%Y")
            elif attr == "_visible":
                # Spelled out rather than left blank. An empty cell in a column
                # headed "Visible on site" reads as missing data, and this is
                # the column somebody sorts on first.
                v = "yes" if v else "NO"
            elif isinstance(v, bool):
                v = "yes" if v else ""
            line.append(v)
        ws2.append(line)
    for i, (_, attr) in enumerate(COLUMNS, start=1):
        letter = ws2.cell(row=1, column=i).column_letter
        if attr in _MONEY:
            for c in ws2[letter][1:]:
                c.number_format = "#,##0"
        ws2.column_dimensions[letter].width = 16 if attr not in (
            "address", "url", "image_url", "hold_reason", "_why_hidden",
            "best_strategy", "deal_block_reason") else 34
    if rows:
        ws2.auto_filter.ref = ws2.dimensions
    ws2.freeze_panes = "C2"

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
