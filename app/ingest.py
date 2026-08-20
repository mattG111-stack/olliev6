"""CSV ingestion service.

Takes raw scraper CSVs (for-sale, sold, rent), runs the pricing pipeline,
writes results to the DB under a new import_batch_id, archives the prior batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path

import re

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .models import (
    BatchType,
    ImportBatch,
    IngestJob,
    PropertyForSale,
    PropertyRent,
    PropertySold,
)
from .pricing import assumptions as A
from .pricing import zones as Z
from .pricing.cashflow import RentRates
from .pricing.comps import SoldDataset
from .pricing.glm import canonical_type
from .pricing.pool import detect_pool
from .pricing.pipeline import run as run_pipeline

# Remote / non-comparable locations excluded from ingest entirely — no comparable
# sales and no road access, so nothing can price them credibly. Matched (substring,
# lowercased) against the listing's "suburb + address". Add more here as needed.
_EXCLUDED_LOCATIONS = ("kawau island",)


# The scraper has emitted more than one column layout. The newer export uses
# rawer names (last_sale_price, bedrooms, full_address) where the older one used
# the portal's own (price_numeric, key_bedrooms, address). Nothing downstream
# should have to know which file it is looking at, so the names are normalised
# once, here, on the way in.
#
# Left = what a file might call it. Right = what this module reads.
# An alias is only applied when the canonical column is ABSENT, so a file that
# already uses the canonical names is untouched.
COLUMN_ALIASES: dict[str, str] = {
    "full_address": "address",
    "address_slug": "slug_id",
    "last_sale_price": "price_numeric",
    "last_sale_date": "sold_listing_date",
    "bedrooms": "key_bedrooms",
    "bathrooms": "key_bathrooms",
    "parking_total": "key_carspaces",
    "floor_area": "key_floor_area",
    "land_area": "key_land_area",
    "cv_capital_value": "cv_numeric",
    "cv_land_value": "land_value_numeric",
    "cv_improvements_value": "improvement_value_numeric",
    "cv_valuation_date": "valuation_last_date",
    "sales_history_json": "sale_history_json",
    "council_evaluations_json": "cv_history_json",
    "photo_primary_url": "image_1_url",
    "avm_mid": "property_valuation_numeric",
    "avm_low": "property_valuation_low_numeric",
    "avm_high": "property_valuation_high_numeric",
    # The export has used both spellings across samples.
    "listing_price": "placeholder_asking",
    "listed_price": "placeholder_asking",
}

# Location columns that arrive slugified ("blockhouse-bay") in the newer export.
# Stored verbatim, they would never match a listing's "Blockhouse Bay", and the
# failure is SILENT: comp matching simply finds nothing and every row prices off
# a thinner sample. Worth un-slugging on the way in rather than discovering it
# as a mysteriously bad valuation months later.
_SLUGGED_LOCATION_COLUMNS = ("suburb", "district", "region")

# A sale outside this band of its council valuation is not a market transaction.
# Kept in step with routers.properties.ARMS_LENGTH_LO/HI, which guards the same
# thing on the way out for data loaded before this check existed.
ARMS_LENGTH_LO, ARMS_LENGTH_HI = 0.3, 3.0


def _deslug(v):
    """'blockhouse-bay' -> 'Blockhouse Bay'. Anything already spaced is left alone."""
    if not isinstance(v, str):
        return v
    s = v.strip()
    # Only touch values that are actually slugs: hyphenated, no spaces. A real
    # suburb with a hyphen in it ("Point Chevalier - West") has spaces too.
    if not s or " " in s or "-" not in s:
        return v
    return s.replace("-", " ").title()


def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Accept either scraper layout, and un-slug location names.

    Returns a new frame; the caller's is not mutated.
    """
    if df is None or df.empty and not len(df.columns):
        return df
    have = set(df.columns)
    rename = {
        src: dst for src, dst in COLUMN_ALIASES.items()
        if src in have and dst not in have
    }
    out = df.rename(columns=rename) if rename else df.copy()
    for col in _SLUGGED_LOCATION_COLUMNS:
        if col in out.columns:
            out[col] = out[col].map(_deslug)
    return out


@dataclass
class IngestResult:
    batch_type: str
    batch_id: int
    rows_inserted: int
    rows_rejected: int
    notes: str = ""
    audit_warnings_json: str | None = None  # set by for-sale audit; surfaced on admin UI


def _parse_area(v):
    if pd.isna(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().replace(",", "")
    digits = ""
    for c in s:
        if c.isdigit() or c == ".":
            digits += c
        elif digits:
            break
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def _to_int(v):
    try:
        f = float(v)
        if f != f:
            return None
        return int(f)
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _to_str(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    return str(v).strip() or None


def _archive_prior(db: Session, batch_type: str, region: str) -> None:
    prior = (
        db.query(ImportBatch)
        .filter(ImportBatch.batch_type == batch_type, ImportBatch.region == region, ImportBatch.is_active.is_(True))
        .all()
    )
    for p in prior:
        p.is_active = False
        p.status = "archived"


def _dedupe_by_slug(df: pd.DataFrame, *, keep_repeat_sales: bool = False) -> tuple[pd.DataFrame, int]:
    """Drop duplicate rows within a single upload, keeping the LAST occurrence
    (the assumption being later rows in the CSV are newer/more up-to-date scrapes).

    keep_repeat_sales widens the key from slug_id to (slug_id, sold date). For a
    for-sale or rent file one row per property is right — the newest scrape wins.
    For SOLD history it is wrong: a house that sold in 2020 and again in 2025 is
    two sales and two comps, and keying on slug_id alone silently throws one away.
    That only started to matter once sold files began covering several years.

    Returns the de-duped frame plus a count of rows dropped.
    """
    if "slug_id" not in df.columns:
        return df, 0
    before = len(df)
    key = ["slug_id"]
    if keep_repeat_sales and "sold_listing_date" in df.columns:
        key.append("sold_listing_date")
    # Rows with no slug_id can't collide with anything, so they are set aside
    # rather than dropped. The previous version filtered them out first and then
    # tried to add them back from the already-filtered frame, which always came
    # up empty — every slug-less row was being discarded in silence.
    no_slug = df[df["slug_id"].isna()]
    slugged = df.dropna(subset=["slug_id"]).drop_duplicates(subset=key, keep="last")
    deduped = pd.concat([slugged, no_slug], ignore_index=True) if len(no_slug) else slugged
    return deduped, max(0, before - len(deduped))


def _prune_old_batches(db: Session, batch_type: str, region: str, *, keep_last: int) -> int:
    """Delete batches beyond the retention window (oldest first). Returns count pruned.
    Cascades to all property rows tagged with those batch_ids via ON DELETE CASCADE."""
    if keep_last <= 0:
        return 0
    keep_ids = [
        row.id
        for row in db.query(ImportBatch.id)
        .filter(ImportBatch.batch_type == batch_type, ImportBatch.region == region)
        .order_by(ImportBatch.created_at.desc())
        .limit(keep_last)
        .all()
    ]
    if not keep_ids:
        return 0
    to_delete = (
        db.query(ImportBatch)
        .filter(ImportBatch.batch_type == batch_type, ImportBatch.region == region, ~ImportBatch.id.in_(keep_ids))
        .all()
    )
    n = len(to_delete)
    # Need to manually remove rows from child tables because we don't have ON DELETE CASCADE on the FK.
    for b in to_delete:
        if batch_type == BatchType.FOR_SALE.value:
            db.query(PropertyForSale).filter(PropertyForSale.import_batch_id == b.id).delete(synchronize_session=False)
        elif batch_type == BatchType.SOLD.value:
            db.query(PropertySold).filter(PropertySold.import_batch_id == b.id).delete(synchronize_session=False)
        elif batch_type == BatchType.RENT.value:
            db.query(PropertyRent).filter(PropertyRent.import_batch_id == b.id).delete(synchronize_session=False)
        # An ingest_job may reference this batch (FK with no cascade) — detach it
        # first so the batch delete doesn't violate the foreign key.
        db.query(IngestJob).filter(IngestJob.batch_id == b.id).update(
            {IngestJob.batch_id: None}, synchronize_session=False
        )
        db.delete(b)
    return n


# A price the listing actually DISPLAYS: "$1,250,000", "$1.25m", "Enquiries over
# $640,000". Requires a currency marker, so "Deadline Sale 12 June 2026" is not
# mistaken for a price because it happens to contain digits.
_SHOWN_PRICE = re.compile(r"\$\s?\d|(?<!\w)\d[\d,.]*\s?[mk](?!\w)", re.I)

# Phrases that mean "no advertised price", in the order we test them.
_NO_PRICE_PHRASES = (
    ("auction",     ("auction",)),
    ("tender",      ("tender", "deadline", "dutch auction")),
    ("negotiation", ("negotia", "by neg", "poa", "price on application",
                     "contact agent", "enquiries welcome", "expressions of interest",
                     "eoi", "offers invited", "buyer enquiry", "asking price on request",
                     "set date", "for sale")),
)


def _detect_listing_type(price_display: str | None, price_numeric) -> str:
    """Classify a listing's sale method. Returns: fixed | auction | tender |
    negotiation | unknown.

    Only `fixed` is allowed to drive the asking x 0.95 price path, so the whole
    job of this function is deciding whether the scraped number is a price the
    vendor is actually asking.

    It usually is not. A "Price by Negotiation" listing still carries a number in
    the feed: portals let agents set a hidden SEARCH PRICE so the listing appears
    in buyers' price filters. It is set low on purpose to catch more searches, and
    it is not an asking price. 48A Garnet Road, Westmere is advertised by
    negotiation everywhere and arrived here as "$2,350,000" against a $3.5M CV —
    which we then published as a valuation and a 49% margin.

    This function used to return `fixed` for any row with a number, testing that
    before it ever looked at the words, so every negotiation and tender listing
    with a search price took the asking path and the phrase checks below were
    unreachable. The order is now: what the listing SAYS decides, and the number
    is trusted only when the listing displays one.
    """
    s = (str(price_display).strip().lower() if price_display else "")
    try:
        has_number = price_numeric is not None and float(price_numeric or 0) > 0
    except (TypeError, ValueError):
        has_number = False

    # Auction first: an auction listing showing a figure is showing RV/CV.
    if "auction" in s:
        return "auction"

    # A displayed price is a real price, qualifier words and all — "$829,000
    # Negotiable" and "Enquiries over $640,000" are both genuine numbers.
    if _SHOWN_PRICE.search(s):
        return "fixed"

    # Words but no figure: the listing is telling us there is no asking price.
    for kind, needles in _NO_PRICE_PHRASES:
        if any(n in s for n in needles):
            return kind

    # No display text at all. The number is the only evidence there is, so it
    # stands — this is the plain-price feed, not a price-withheld listing.
    if not s and has_number:
        return "fixed"

    # Text we do not recognise, and no figure in it. Refuse to guess: treating
    # this as an asking price is how a search price becomes a valuation.
    return "unknown"


def _to_bool(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "y", "t"):
        return True
    if s in ("false", "0", "no", "n", "f"):
        return False
    return None


def _collect_images(row: dict) -> str | None:
    """image_1_url .. image_30_url -> one newline-separated string.

    Stops at the first gap: the scrape numbers them contiguously, so a missing
    slot marks the end of the gallery rather than a hole in it.
    """
    urls = []
    for i in range(1, 31):
        u = _to_str(row.get(f"image_{i}_url"))
        if not u:
            break
        urls.append(u)
    return "\n".join(urls) if urls else None


def _common_property_payload(row: dict, *, region: str = "Auckland") -> dict:
    return {
        "address": _to_str(row.get("address")),
        "name": _to_str(row.get("name")),
        "suburb": _to_str(row.get("suburb")),
        "district": _to_str(row.get("district")),
        "region": _to_str(row.get("region")) or region,
        "postcode": _to_str(row.get("postcode")),
        "latitude": _to_float(row.get("latitude")),
        "longitude": _to_float(row.get("longitude")),
        "property_type": _to_str(row.get("property_type")),
        "type_of_title": _to_str(row.get("type_of_title")),
        # Repair known-mislabelled scrape zonings (e.g. large Waiuku lifestyle
        # blocks tagged "Mixed Housing Suburban") so the stored zone matches what
        # the subdivision engine now uses. See zones.corrected_zoning.
        "zoning": Z.corrected_zoning(
            _to_str(row.get("zoning")), suburb=_to_str(row.get("suburb")),
            land_area=_parse_area(row.get("key_land_area")), address=_to_str(row.get("address"))),
        "land_slope_contour": _to_str(row.get("land_slope_contour")),
        "beds": _to_int(row.get("key_bedrooms")),
        "baths": _to_int(row.get("key_bathrooms")),
        "cars": _to_int(row.get("key_carspaces")),
        "floor_area_m2": _parse_area(row.get("key_floor_area")),
        "land_area_m2": _parse_area(row.get("key_land_area")),
        "cv_numeric": _to_float(row.get("cv_numeric") or row.get("property_valuation_cv_numeric")),
        "land_value_numeric": _to_float(row.get("land_value_numeric")),
        "improvement_value_numeric": _to_float(row.get("improvement_value_numeric")),
        "url": _to_str(row.get("url")),
        "slug_id": _to_str(row.get("slug_id")),
        "image_url": _to_str(row.get("image_1_url")),
        "image_urls": _collect_images(row),
        "image_count": _to_int(row.get("image_count")),
        "listing_date": _to_str(row.get("listing_date")),
        "days_on_market": _to_float(row.get("days_on_market")),
        # Raw scraper fields preserved
        "key_facts": _to_str(row.get("key_facts")),
        "key_time_on_market": _to_str(row.get("key_time_on_market")),
        "estate_description": _to_str(row.get("estate_description")),
        "council_valuation_summary": _to_str(row.get("council_valuation_summary")),
        "property_trend": _to_str(row.get("property_trend")),
        "sale_status": _to_str(row.get("sale_status")),
        "last_updated": _to_str(row.get("last_updated")),
        # Third-party reference valuation from the source portal
        "third_party_valuation": _to_float(row.get("property_valuation_numeric")),
        "third_party_valuation_high": _to_float(row.get("property_valuation_high_numeric")),
        "third_party_valuation_low": _to_float(row.get("property_valuation_low_numeric")),
        "valuation_last_date": _to_str(row.get("valuation_last_date")),
        # CV change
        "valuation_rateable_change_pct": _to_float(row.get("valuation_rateable_change_pct")),
        "valuation_land_change_pct": _to_float(row.get("valuation_land_change_pct")),
        "valuation_improvement_change_pct": _to_float(row.get("valuation_improvement_change_pct")),
        # Last sale of this property
        "valuation_last_sold_value": _to_float(row.get("valuation_last_sold_value")),
        "valuation_last_sold_date": _to_str(row.get("valuation_last_sold_date")),
        "sold_listing_date": _to_str(row.get("sold_listing_date")),
        "sold_listing_price_label": _to_str(row.get("sold_listing_price_label")),
        # Trend JSONs for charts
        "valuation_trend_yearly_json": _to_str(row.get("valuation_trend_yearly_json")),
        "valuation_trend_monthly_json": _to_str(row.get("valuation_trend_monthly_json")),
        "sale_history_json": _to_str(row.get("sale_history_json")),
        "cv_history_json": _to_str(row.get("cv_history_json")),
        "schools_json": _to_str(row.get("schools_json")),
        # Agent contacts
        "agent1_name": _to_str(row.get("agent1_name")),
        "agent1_phone": _to_str(row.get("agent1_phone")),
        "agent1_email": _to_str(row.get("agent1_email")),
        "agent1_job_title": _to_str(row.get("agent1_job_title")),
        "agent1_company_name": _to_str(row.get("agent1_company_name")),
        "agent2_name": _to_str(row.get("agent2_name")),
        "agent2_phone": _to_str(row.get("agent2_phone")),
        "agent2_email": _to_str(row.get("agent2_email")),
        "agent2_job_title": _to_str(row.get("agent2_job_title")),
        "agent2_company_name": _to_str(row.get("agent2_company_name")),
        "company_name": _to_str(row.get("company_name")),
        # Other potentially-present scraper fields
        "building_age": _to_str(row.get("building_age")),
        "parking_covered": _to_int(row.get("parking_covered")),
        "parking_other": _to_int(row.get("parking_other")),
        # Flag, keyword flag, or the listing text saying so — see detect_pool.
        # Settled here, once, because a pool decides which sales this property
        # can be compared against and whether we offer to sell them one.
        "has_swimming_pool": detect_pool(row),
        "is_new_construction": _to_bool(row.get("is_new_construction")),
        "is_coastal_waterfront": _to_bool(row.get("is_coastal_waterfront")),
        "storey_count": _to_int(row.get("storey_count")),
        "other_features": _to_str(row.get("other_features")),
        "description": _to_str(row.get("description")),
        "listing_title": _to_str(row.get("listing_title")),
        "listing_published_date": _to_str(row.get("listing_published_date")),
    }


def _explode_sale_history(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """One row per SALE, not one per property. Returns (frame, sales_added).

    The newer sold export is keyed on the property and carries every sale it
    knows about in sale_history_json, with only the latest one lifted into the
    top-level price/date columns. Taking just those columns throws the rest
    away: a 14-row sample held 23 sales, so 10 of 23 comps were being discarded
    at the door.

    Property attributes (beds, floor area, CV) are as they are TODAY, not as at
    the sale date, and get copied onto every expanded row. That is tolerable
    because the comp engine only looks at recent sales — see
    assumptions.COMP_MAX_AGE_YEARS — but it is the reason this must never be
    paired with an unbounded date range.
    """
    if "sale_history_json" not in df.columns:
        return df, 0

    import json

    rows: list[dict] = []
    added = 0
    for _, row in df.iterrows():
        base = row.to_dict()
        raw = base.get("sale_history_json")
        history = []
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    history = parsed
            except (ValueError, TypeError):
                history = []
        # Nothing usable, or a single sale already represented by the top-level
        # columns — leave the row exactly as it came in.
        if len(history) <= 1:
            rows.append(base)
            continue
        for sale in history:
            if not isinstance(sale, dict):
                continue
            price = _to_float(sale.get("salePrice"))
            when = _to_str(sale.get("saleDate"))
            if not price or not when:
                continue
            r = dict(base)
            r["price_numeric"] = price
            r["sold_listing_date"] = when
            rows.append(r)
            added += 1
        # If the history parsed but yielded nothing usable, keep the original.
        if not any(isinstance(s, dict) and s.get("salePrice") for s in history):
            rows.append(base)
    if not rows:
        return df, 0
    out = pd.DataFrame(rows)
    # How many rows the expansion ADDED. Counting the sales lifted out of the
    # JSON instead gets this wrong, because single-sale rows pass through
    # without contributing to that tally.
    return out, max(0, len(out) - len(df))


_ISO_ISH = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}")


def canonical_sale_date(v, *, month_first: bool = False) -> str | None:
    """A sale date as plain YYYY-MM-DD, whatever shape it arrived in.

    The same sale reaches us spelled several ways depending on the file:
    '2026-03-18 00:00:00' from an Excel timestamp, '2026-03-18' from a CSV,
    '18/03/2026' when someone has formatted the column. Compared as raw strings
    those are three different sales, so one sale ends up stored three times and
    counted three times as a comp.

    Day-first parsing by default: this is NZ data, so 03/04/2026 is 3 April.
    `month_first` is for a source that writes the American order — Trade Me's
    export does, and read day-first every date with a day of 12 or lower
    transposes silently. Two rows in five, each landing in a different month,
    with nothing raised.

    Unparseable values are handed back stripped rather than dropped — a date
    this cannot read is still better kept than silently discarded.
    """
    s = _to_str(v)
    if not s:
        return None
    # Year-first values are unambiguous, and passing dayfirst for them makes
    # pandas warn on every row — which on a 14,000-row file is 14,000 warnings
    # in the deploy log.
    day_first = not month_first and not _ISO_ISH.match(s)
    try:
        parsed = pd.to_datetime(s, errors="coerce", dayfirst=day_first)
    except (ValueError, TypeError):
        return s
    if parsed is None or parsed is pd.NaT or pd.isna(parsed):
        return s
    return parsed.strftime("%Y-%m-%d")


def dedupe_stored_sales(db: Session, region: str) -> int:
    """Delete duplicate sold rows already in the database. Returns rows removed.

    Skipping duplicates at insert stops NEW ones arriving; it does nothing about
    the ones already stored. Every sold load before append mode existed replaced
    the previous batch rather than merging, so any overlap between files landed
    as genuine duplicate rows — and a duplicated sale is a duplicated comp,
    quietly double-weighting whatever sold twice.

    A duplicate is the same property on the same date: (slug_id, sold_date).
    Rows with no slug_id are left alone — nothing identifies them well enough to
    call two of them the same sale.

    The HIGHEST id wins. Later loads carry refreshed attributes (an updated CV,
    a corrected floor area), so the newest copy of a sale is the better one.
    """
    rows = (
        db.query(PropertySold.id, PropertySold.slug_id, PropertySold.sold_date)
        .join(ImportBatch, PropertySold.import_batch_id == ImportBatch.id)
        .filter(ImportBatch.region == region,
                ImportBatch.batch_type == BatchType.SOLD.value)
        .order_by(PropertySold.id)
        .all()
    )
    keep: dict[tuple[str, str], int] = {}
    drop: list[int] = []
    restamp: list[tuple[int, str]] = []
    for rid, slug, date in rows:
        if not slug:
            continue
        canon = canonical_sale_date(date)
        # Rows stored before dates were canonicalised carry whatever spelling
        # their file used. Comparing on the canonical form is what lets those
        # collapse; rewriting the survivor stops the mixture persisting.
        if canon and canon != date:
            restamp.append((rid, canon))
        key = (slug, canon or "")
        if key in keep:
            drop.append(keep[key])   # the earlier one loses
        keep[key] = rid
    survivors = set(keep.values())
    for rid, canon in restamp:
        if rid in survivors:
            db.query(PropertySold).filter(PropertySold.id == rid).update(
                {PropertySold.sold_date: canon}, synchronize_session=False)
    if not drop:
        return 0
    # Chunked: SQLite caps how many values an IN (...) may carry, and a large
    # historical file can produce more duplicates than that limit.
    removed = 0
    for i in range(0, len(drop), 500):
        removed += (
            db.query(PropertySold)
            .filter(PropertySold.id.in_(drop[i:i + 500]))
            .delete(synchronize_session=False)
        )
    return removed


def sold_batch_ids(db: Session, region: str) -> list[int]:
    """Every sold batch that counts as live for a region.

    A sold batch is a DELIVERY, not the dataset. That distinction did not exist
    while each upload replaced the last, so a dozen places independently
    resolved "the sold data" as "the newest batch" and were right. Once history
    began accumulating every one of them silently narrowed to whatever arrived
    most recently — load a small weekly file over years of history and comps,
    valuations and dashboards all ran against the small file, with no error and
    no visible sign the rest had dropped out.

    One helper so the next reader cannot get it wrong in a new way.
    """
    return [b.id for b in db.query(ImportBatch.id).filter(
        ImportBatch.batch_type == BatchType.SOLD.value,
        ImportBatch.region == region,
        ImportBatch.status.in_(("staged", "published")),
    ).all()]


def _sold_keys_on_file(db: Session, region: str) -> set[tuple[str, str]]:
    """(slug_id, sold_date) of every sold row already held for this region.

    A sale is a historical fact: the same one arriving in a second file is the
    same sale, not a second one. Without this, re-uploading a file — or loading
    two date-ranged exports that overlap at the join — would double every comp
    in the overlap and quietly drag valuations toward whatever sold twice.
    """
    rows = (
        db.query(PropertySold.slug_id, PropertySold.sold_date)
        .join(ImportBatch, PropertySold.import_batch_id == ImportBatch.id)
        .filter(
            ImportBatch.batch_type == BatchType.SOLD.value,
            ImportBatch.region == region,
            ImportBatch.status.in_(("staged", "published")),
        )
        .all()
    )
    return {(s or "", canonical_sale_date(d) or "") for s, d in rows}


# ---- Sold ingestion ----
def ingest_sold(db: Session, sold_df: pd.DataFrame, filename: str, *, region: str = "Auckland", uploaded_by_id: int | None = None, publish: bool = True, append: bool = True) -> IngestResult:
    """Load a sold file.

    append=True (the default) ADDS to the sold history rather than replacing it.
    Sold data is cumulative by nature — a 2019-2023 export and a 2023-2026 export
    are two parts of one dataset, and last month's sales do not stop being true
    when this month's file arrives. Prior batches stay active, nothing is pruned,
    and rows already on file are skipped rather than duplicated.

    append=False restores the original replace-the-previous-batch behaviour.
    """
    sold_df = normalise_columns(sold_df)
    sold_df, expanded = _explode_sale_history(sold_df)
    sold_df, dropped = _dedupe_by_slug(sold_df, keep_repeat_sales=True)
    if publish and not append:
        _archive_prior(db, BatchType.SOLD.value, region)
    batch = ImportBatch(
        batch_type=BatchType.SOLD.value, region=region, filename=filename,
        rows_total=len(sold_df), is_active=publish,
        status="published" if publish else "staged",
        published_at=(func.now() if publish else None),
        uploaded_by_id=uploaded_by_id,
        note=(f"deduped {dropped} duplicate sales" if dropped else None),
    )
    db.add(batch); db.flush()

    seen = _sold_keys_on_file(db, region) if append else set()
    inserted = 0; rejected = 0; already = 0; not_arms_length = 0
    span_lo = span_hi = None
    for _, row in sold_df.iterrows():
        payload = _common_property_payload(row.to_dict(), region=region)
        sale_price = _to_float(row.get("price_numeric"))
        # A sold row is useless for comp matching without suburb + price.
        # Also drop bedless/floorless rows — they can't be matched against anyway.
        if not payload.get("suburb") or not sale_price or sale_price < 10000:
            rejected += 1; continue
        if payload.get("beds") is None and payload.get("floor_area_m2") is None:
            rejected += 1; continue
        # Not a market sale. A price this far from the council valuation is a
        # part share, a transfer between relatives, or a CV attached to the wrong
        # property — it is a record of something, but not of what a house is
        # worth. One such row reached a suburb panel as "Tender: -93.4% vs CV"
        # and decided which sale method ranked best, because in a thin suburb a
        # single sale IS the median. Rejected here so no reader has to remember
        # to guard against it.
        cv = payload.get("cv_numeric")
        if cv and cv > 0 and not (ARMS_LENGTH_LO <= sale_price / cv <= ARMS_LENGTH_HI):
            rejected += 1; not_arms_length += 1; continue
        sold_date = canonical_sale_date(row.get("sold_listing_date"))
        key = (payload.get("slug_id") or "", sold_date or "")
        # Only skip when the key is real. Two slug-less rows would otherwise
        # collide on ("", "") and the second would look like a duplicate.
        if append and key != ("", "") and key in seen:
            already += 1; continue
        seen.add(key)
        if sold_date:
            span_lo = sold_date if span_lo is None else min(span_lo, sold_date)
            span_hi = sold_date if span_hi is None else max(span_hi, sold_date)
        rec = PropertySold(
            **payload,
            import_batch_id=batch.id,
            sale_price=sale_price,
            sold_date=sold_date,
            sale_method=_to_str(row.get("sale_method")),
        )
        db.add(rec); inserted += 1
        if inserted % 500 == 0:
            db.flush()

    batch.rows_inserted = inserted; batch.rows_rejected = rejected + already
    # Sweep duplicates left by loads made before append mode existed. Cheap when
    # there is nothing to do, and it means the table converges on clean rather
    # than needing a separate maintenance step someone has to remember.
    purged = dedupe_stored_sales(db, region) if append else 0

    notes = [batch.note] if batch.note else []
    if purged:
        notes.append(f"removed {purged} duplicate sales already stored")
    if expanded:
        notes.append(f"{expanded} extra sales from sale history")
    if span_lo and span_hi:
        notes.append(f"covers {span_lo[:10]} to {span_hi[:10]}")
    if already:
        notes.append(f"{already} sales already on file")
    if not_arms_length:
        notes.append(f"{not_arms_length} rejected as not market sales")
    # Pruning deletes whole batches beyond the retention window. That is right
    # when each upload supersedes the last; under append it would delete the
    # oldest years of history, so it is skipped.
    if not append:
        pruned = _prune_old_batches(db, BatchType.SOLD.value, region, keep_last=settings.batch_retention_limit)
        if pruned:
            notes.append(f"pruned {pruned} old batches")
    batch.note = " · ".join(notes) if notes else None
    db.commit()
    return IngestResult(BatchType.SOLD.value, batch.id, inserted, rejected + already, batch.note or "")


# ---- Rent ingestion ----
def ingest_rent(db: Session, rent_df: pd.DataFrame, filename: str, *, region: str = "Auckland", uploaded_by_id: int | None = None) -> IngestResult:
    rent_df = normalise_columns(rent_df)
    rent_df, dropped = _dedupe_by_slug(rent_df)
    _archive_prior(db, BatchType.RENT.value, region)
    batch = ImportBatch(
        batch_type=BatchType.RENT.value, region=region, filename=filename,
        rows_total=len(rent_df), is_active=True, status="published",
        published_at=func.now(), uploaded_by_id=uploaded_by_id,
        note=(f"deduped {dropped} duplicate slug_ids" if dropped else None),
    )
    db.add(batch); db.flush()

    inserted = 0; rejected = 0
    for _, row in rent_df.iterrows():
        payload = _common_property_payload(row.to_dict(), region=region)
        weekly_rent = _to_float(row.get("price_numeric"))
        # A rental row is useless without suburb + rent + at least one of beds/floor.
        if not payload.get("suburb") or not weekly_rent or weekly_rent < 100 or weekly_rent > 10000:
            rejected += 1; continue
        if payload.get("beds") is None and payload.get("floor_area_m2") is None:
            rejected += 1; continue
        rec = PropertyRent(
            **payload,
            import_batch_id=batch.id,
            weekly_rent=weekly_rent,
            listing_date_rent=_to_str(row.get("listing_date")),
        )
        db.add(rec); inserted += 1
        if inserted % 500 == 0:
            db.flush()

    batch.rows_inserted = inserted; batch.rows_rejected = rejected
    pruned = _prune_old_batches(db, BatchType.RENT.value, region, keep_last=settings.batch_retention_limit)
    if pruned:
        batch.note = (batch.note or "") + f" · pruned {pruned} old batches"
    db.commit()
    return IngestResult(BatchType.RENT.value, batch.id, inserted, rejected, batch.note or "")


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and v != v) or v in ("", 0, "0", "nan")


def _needs_lookup(row) -> bool:
    """A row needs a CoreLogic lookup when a pricing-critical field is blank.

    Floor and land drive the size checks; the CV is the primary pricing anchor
    (`ollie_value = cv_v × min(ratio, RATIO_CAP)`). A row with floor and land but
    a blank CV used to be skipped even though its valuation had no anchor — so a
    blank CV now triggers a lookup as well."""
    return (_blank(row.get("key_floor_area"))
            or _blank(row.get("key_land_area"))
            or _blank(row.get("cv_numeric")))


def _fill_df_from_corelogic(df: pd.DataFrame, *, delay: float = 0.5, cap: int = 20000,
                            progress_cb=None) -> tuple[int, int]:
    """Overlay CoreLogic (propertyvalue.co.nz) data onto the pipeline INPUT for
    listings missing a floor area, land area or CV — the pricing-critical fields —
    BEFORE the pipeline runs, so the valuation is computed on the filled numbers.

    Only looks up rows that are actually missing one of those fields (a bounded
    subset). Throttled, circuit-breaks if CoreLogic starts refusing, and degrades
    to a no-op if it's unreachable (the row just prices on what it has, as before).

    The cap is a runaway backstop, not a work limit: it is set comfortably above a
    full weekly batch (~14k rows) so enrich finishes in one pass. Resumability —
    re-running only fills what is still blank — bounds the real work now, and the
    40-consecutive-miss circuit breaker still guards against a rate-limit storm.

    `progress_cb(looked, need, filled, misses)` is called periodically so a caller
    can persist durable progress (rows processed / filled / missed) to the DB while
    the stage runs, instead of the state living only in this process's stdout.

    Returns (cells_filled, rows_looked_up)."""
    import time
    from .propertyvalue import pv_lookup

    pairs = (("key_floor_area", "floor_area_m2"), ("key_land_area", "land_area_m2"),
             ("key_bedrooms", "beds"), ("key_bathrooms", "baths"),
             ("cv_numeric", "cv"), ("zoning", "zoning"))
    filled = looked = consec_fail = misses = 0

    # How many rows will actually need a lookup, so the log shows progress against
    # a real denominator rather than an unknown. Counted up front — it is a cheap
    # pass over the frame and makes "1400/4820" legible in the deploy log.
    need = sum(
        1 for _, r in df.iterrows()
        if _needs_lookup(r) and not _blank(r.get("address"))
    )
    target = min(need, cap)
    t0 = time.time()
    print(f"  [ingest] CoreLogic fill starting: {need} rows need a lookup, "
          f"cap={cap}, will attempt {target} at {delay}s each "
          f"(~{target * delay / 60:.0f} min minimum)", flush=True)

    for idx, row in df.iterrows():
        if looked >= cap:
            print(f"  [ingest] CoreLogic fill hit the cap of {cap} lookups", flush=True)
            break
        # Only spend a lookup when a pricing-critical field (floor, land or CV) is
        # missing — see _needs_lookup.
        if not _needs_lookup(row):
            continue
        addr = row.get("address")
        if _blank(addr):
            continue
        q = ", ".join(x for x in (str(addr), str(row.get("suburb") or "").strip(), "Auckland")
                      if x and x.lower() != "nan")
        looked += 1
        try:
            pv = pv_lookup(q)
        except Exception:
            pv = None
        if not pv:
            misses += 1
            consec_fail += 1
            if progress_cb is not None:
                try:
                    progress_cb(looked, need, filled, misses)
                except Exception:
                    pass
            if consec_fail >= 40:
                print(f"  [ingest] CoreLogic fill stopped after {consec_fail} misses "
                      f"(likely rate-limited) at lookup {looked}/{target}", flush=True)
                break
            time.sleep(delay)
            continue
        consec_fail = 0
        for dfcol, pvkey in pairs:
            if _blank(row.get(dfcol)) and pv.get(pvkey):
                df.at[idx, dfcol] = pv.get(pvkey)
                filled += 1

        # Heartbeat. Without this the loop is silent for up to 45 minutes and a
        # crash is indistinguishable from still-running. Every 25 lookups we
        # print position, hit rate and elapsed time, so the deploy log shows
        # exactly how far it got before it stopped.
        if looked % 25 == 0:
            elapsed = time.time() - t0
            rate = looked / elapsed if elapsed > 0 else 0
            remaining = (target - looked) / rate / 60 if rate > 0 else 0
            print(f"  [ingest] CoreLogic {looked}/{target} lookups, "
                  f"{filled} cells filled, {elapsed / 60:.1f} min elapsed, "
                  f"~{remaining:.0f} min left", flush=True)
            if progress_cb is not None:
                try:
                    progress_cb(looked, need, filled, misses)
                except Exception:
                    pass

        time.sleep(delay)

    elapsed = time.time() - t0
    print(f"  [ingest] CoreLogic fill FINISHED: {looked} lookups, "
          f"{filled} cells filled, {elapsed / 60:.1f} min total", flush=True)
    return filled, looked


# ---- For-sale ingestion (runs the full pricing pipeline) ----
def ingest_for_sale(
    db: Session,
    for_sale_df: pd.DataFrame,
    sold_df: pd.DataFrame,
    filename: str,
    *,
    region: str = "Auckland",
    uploaded_by_id: int | None = None,
    publish: bool = True,
    fill_missing: bool = False,
) -> IngestResult:
    for_sale_df = normalise_columns(for_sale_df)
    sold_df = normalise_columns(sold_df)
    for_sale_df, dropped = _dedupe_by_slug(for_sale_df)
    # Fill blank sizes from CoreLogic BEFORE pricing so the valuation uses them.
    if fill_missing:
        cells, looked = _fill_df_from_corelogic(for_sale_df)
        print(f"  [ingest] CoreLogic pre-fill: {cells} cells filled across {looked} listings")
    if publish:
        _archive_prior(db, BatchType.FOR_SALE.value, region)
    batch = ImportBatch(
        batch_type=BatchType.FOR_SALE.value, region=region, filename=filename,
        rows_total=len(for_sale_df), is_active=publish,
        status="published" if publish else "staged",
        published_at=(func.now() if publish else None),
        uploaded_by_id=uploaded_by_id,
        note=(f"deduped {dropped} duplicate slug_ids" if dropped else None),
    )
    db.add(batch); db.flush()

    # Rental comps from the active rent batch, if one has been uploaded. Without
    # it the yield falls back to the flat CV tier and cashflow cannot vary
    # independently of price.
    rent_rows = (
        db.query(PropertyRent)
        .join(ImportBatch, ImportBatch.id == PropertyRent.import_batch_id)
        .filter(ImportBatch.batch_type == BatchType.RENT.value,
                ImportBatch.region == region, ImportBatch.is_active.is_(True))
        .all()
    )
    rent_rates = None
    if rent_rows:
        rent_rates = RentRates(pd.DataFrame([{
            "weekly_rent": x.weekly_rent, "suburb": x.suburb, "district": x.district,
            "property_type": x.property_type, "beds": x.beds,
        } for x in rent_rows]))
        print(f"  [ingest] rental comps: {len(rent_rows)} active rentals")
    else:
        print("  [ingest] no active rent batch - yields fall back to the CV tier")

    print(f"  [ingest] running pricing pipeline on {len(for_sale_df)} rows ...")
    sold = SoldDataset(sold_df)
    enriched = run_pipeline(for_sale_df, sold, rent_rates)

    print(f"  [ingest] writing {len(enriched)} rows to DB ...")
    inserted = 0; rejected = 0
    rejected_reasons: dict[str, int] = {}
    for _, row in enriched.iterrows():
        payload = _common_property_payload(row.to_dict(), region=region)
        asking = _to_float(row.get("price_numeric"))

        # Fix 3 — drop non-Auckland rows. Our sold dataset is Auckland-only,
        # so comp-matching against listings in Christchurch/Wellington gives garbage.
        if payload.get("region") and payload["region"] != region:
            rejected += 1
            rejected_reasons["non_target_region"] = rejected_reasons.get("non_target_region", 0) + 1
            continue
        # No suburb -> can't comp-match
        if not payload.get("suburb"):
            rejected += 1
            rejected_reasons["no_suburb"] = rejected_reasons.get("no_suburb", 0) + 1
            continue
        # Excluded locations — remote / non-comparable areas (e.g. Kawau Island):
        # no road access, no comparable sales, so no method can price them. Dropped
        # at the door so they never surface.
        _loc = f"{payload.get('suburb') or ''} {payload.get('address') or ''}".lower()
        if any(x in _loc for x in _EXCLUDED_LOCATIONS):
            rejected += 1
            rejected_reasons["excluded_location"] = rejected_reasons.get("excluded_location", 0) + 1
            continue
        # Exclude apartments — their $/m² is too noisy (floor level, view, aspect
        # and body-corp aren't captured), so every valuation method is unreliable
        # for them. Dropped at the door so they never surface as opportunities.
        if canonical_type(payload.get("property_type")) == "Apartment":
            rejected += 1
            rejected_reasons["apartment_excluded"] = rejected_reasons.get("apartment_excluded", 0) + 1
            continue
        # No council valuation -> no valuation is possible. Every method we have
        # is anchored on the CV, and the CV-free fallback (type + beds + baths +
        # land/floor within 20%) reaches only a fifth of them and scores 11.5%.
        # 20.6% of the raw feed has no CV; carrying them as listings we can never
        # price adds nothing, so they are dropped at the door.
        if not payload.get("cv_numeric"):
            rejected += 1
            rejected_reasons["no_cv"] = rejected_reasons.get("no_cv", 0) + 1
            continue

        # Incomplete council record: the CV is the land value alone, with no
        # improvement value, on a property that has a building. 26 Sandpiper
        # Avenue asks $4,000,000 against a $14,000 "CV" that is only the dirt.
        # Everything downstream inherits that error, and a listing whose own
        # published figures are that broken is not one to trade off.
        # NB a bare section legitimately has no improvement value — CV == land
        # value is CORRECT for vacant land, and those are valued off the suburb's
        # bare-section $/m2 rate. Only a record with a BUILDING and no improvement
        # value is broken (626 of the 1,267 matches were vacant land).
        _lv = _to_float(row.get("land_value_numeric"))
        _iv = _to_float(row.get("improvement_value_numeric"))
        _cv = payload.get("cv_numeric")
        _is_vacant = A.is_vacant_type(row.get("property_type"))
        # NOT dropped: an incomplete council record still has a real building in a
        # real suburb, and comparable buildings are selling there. The pipeline
        # values these from matched sold prices instead (see matched_sold_price).

        # Asking price and CV wildly apart. Beyond this one of the two published
        # figures is simply wrong and there is no way to tell which — a $4M ask
        # against a $92k CV is not a bargain, it is a broken record. Between 20%
        # and 50% we keep the listing but stop trusting the asking price (see
        # ASK_VS_CV_BAND in pipeline.py); past 50% we drop it entirely.
        if asking and _cv and abs(float(asking) - float(_cv)) > 0.50 * float(_cv):
            rejected += 1
            rejected_reasons["asking_vs_cv_50pct"] = rejected_reasons.get("asking_vs_cv_50pct", 0) + 1
            continue

        # Fix 1 — placeholder asking ($1, $2) from "by negotiation" listings.
        # These cause "underpriced by 1,500,000×" outliers.
        if asking is not None and asking < 10_000:
            rejected += 1
            rejected_reasons["placeholder_asking"] = rejected_reasons.get("placeholder_asking", 0) + 1
            continue
        # Reject totally empty rows — no beds, no floor, no CV, no asking
        if (payload.get("beds") is None and payload.get("floor_area_m2") is None
                and payload.get("cv_numeric") is None and asking is None):
            rejected += 1
            rejected_reasons["empty_row"] = rejected_reasons.get("empty_row", 0) + 1
            continue
        # Incomplete dwelling record: a building with no floor area can't be
        # size-valued and renders as a blank/unrankable row downstream. Bare land
        # legitimately has no floor (valued off the suburb's $/m2), so only
        # non-vacant types are rejected here. This is the ~130 that used to slip
        # through and skew the lists and market insights.
        if payload.get("floor_area_m2") is None and not _is_vacant:
            rejected += 1
            rejected_reasons["dwelling_no_floor"] = rejected_reasons.get("dwelling_no_floor", 0) + 1
            continue
        rec = PropertyForSale(
            **payload,
            import_batch_id=batch.id,
            asking_price=_to_float(row.get("price_numeric")),
            market_value=_to_float(row.get("market_value")),
            predicted_list=_to_float(row.get("predicted_list")),
            predicted_days=_to_float(row.get("predicted_days")),
            comps_used=_to_int(row.get("comps_used")),
            confidence=_to_str(row.get("confidence")),
            pred_vs_cv=_to_float(row.get("pred_vs_cv")),
            pred_vs_listing=_to_float(row.get("pred_vs_listing")),
            # v3.8 diagnostic fields
            pred_v35=_to_float(row.get("pred_v35")),
            pred_v38=_to_float(row.get("pred_v38")),
            z_weight=_to_float(row.get("z_weight")),
            beta_tier=_to_str(row.get("beta_tier")),
            cv_anchor=_to_float(row.get("cv_anchor")),
            cv_ratio_tier=_to_str(row.get("cv_ratio_tier")),
            correction_used=_to_str(row.get("correction_used")),
            # v4 production AVM
            listing_type=_to_str(row.get("listing_type")),
            pricing_path=_to_str(row.get("pricing_path")),
            range_low=_to_float(row.get("range_low")),
            range_high=_to_float(row.get("range_high")),
            subdivision_premium=_to_float(row.get("subdivision_premium")),
            fair_value=_to_float(row.get("fair_value")),
            margin=_to_float(row.get("margin")),
            is_premium=bool(row.get("is_premium", False)),
            buy_price=_to_float(row.get("buy_price")),
            area_value=_to_float(row.get("area_value")),
            comp_tier=_to_int(row.get("comp_tier")),
            comps_matched=_to_int(row.get("comps_matched")),
            min_lot_m2=_to_float(row.get("min_lot_m2")),
            max_addl_lots=_to_float(row.get("max_addl_lots")),
            sections=_to_int(row.get("sections")),
            dwellings=_to_int(row.get("dwellings")),
            section_rate=_to_float(row.get("section_rate")),
            gross_sales=_to_float(row.get("gross_sales")),
            subdivision_profit=_to_float(row.get("subdivision_profit")),
            section_price_per_m2=_to_float(row.get("section_price_per_m2")),
            section_value_method=_to_str(row.get("section_value_method")),
            services_cost=_to_float(row.get("services_cost")),
            total_subdivided_value=_to_float(row.get("total_subdivided_value")),
            uplift_vs_asking=_to_float(row.get("uplift_vs_asking")),
            est_weekly_rent=_to_float(row.get("est_weekly_rent")),
            est_gross_yield=_to_float(row.get("est_gross_yield")),
            annual_gross_rent=_to_float(row.get("annual_gross_rent")),
            annual_net_rent=_to_float(row.get("annual_net_rent")),
            annual_mortgage=_to_float(row.get("annual_mortgage")),
            annual_cashflow=_to_float(row.get("annual_cashflow")),
            cash_on_cash=_to_float(row.get("cash_on_cash")),
            breakeven_deposit_pct=_to_float(row.get("breakeven_deposit_pct")),
            expected_sale=_to_float(row.get("expected_sale")),
            expected_sale_path=_to_str(row.get("expected_sale_path")),
            expected_sale_band=_to_float(row.get("expected_sale_band")),
            opportunity_score=_to_float(row.get("opportunity_score")),
            opportunity_score_pct=_to_float(row.get("opportunity_score_pct")),
            best_strategy=_to_str(row.get("best_strategy")),
            best_net_gain=_to_float(row.get("best_net_gain")),
            is_underpriced=bool(row.get("is_underpriced", False)),
            is_cashflow_positive=bool(row.get("is_cashflow_positive", False)),
            is_subdividable=bool(row.get("is_subdividable", False)),
        )
        db.add(rec); inserted += 1
        if inserted % 500 == 0:
            db.flush()

    batch.rows_inserted = inserted; batch.rows_rejected = rejected
    pruned = _prune_old_batches(db, BatchType.FOR_SALE.value, region, keep_last=settings.batch_retention_limit)
    extra_note_parts = []
    if pruned:
        extra_note_parts.append(f"pruned {pruned} old batches")
    if rejected_reasons:
        extra_note_parts.append("rejected: " + ", ".join(f"{k}={v}" for k, v in rejected_reasons.items()))
    if extra_note_parts:
        batch.note = (batch.note or "") + " · " + " · ".join(extra_note_parts)
    db.commit()

    # Post-ingest audit — runs against the rows we actually INSERTED, not the
    # raw input. Including rejected rows (non-Auckland, no-CV, placeholders)
    # double-counts as "insufficient" and gives misleading high-severity flags
    # for data we never showed to the user.
    from app.pricing.audit import audit_for_sale_batch, serialise
    try:
        inserted_rows = db.query(PropertyForSale).filter(
            PropertyForSale.import_batch_id == batch.id
        ).all()
        audit_input = [
            {
                "address": r.address,
                "suburb": r.suburb,
                "cv_numeric": r.cv_numeric,
                "price_numeric": r.asking_price,
                "market_value": r.market_value,
                "confidence": r.confidence,
            }
            for r in inserted_rows
        ]
        warnings = audit_for_sale_batch(audit_input)
        if warnings:
            print(f"  [audit] {len(warnings)} warning(s) on {len(inserted_rows):,} inserted rows:")
            for w in warnings:
                print(f"    - [{w['severity']}] {w['code']}: {w['message']}")
        warnings_json = serialise(warnings)
    except Exception as e:
        print(f"  [audit] failed: {e}")
        warnings_json = None

    return IngestResult(
        BatchType.FOR_SALE.value, batch.id, inserted, rejected,
        batch.note or "", audit_warnings_json=warnings_json,
    )


def read_csv_bytes(data: bytes | str) -> pd.DataFrame:
    if isinstance(data, bytes):
        return pd.read_csv(BytesIO(data), on_bad_lines="skip")
    return pd.read_csv(StringIO(data), on_bad_lines="skip")
