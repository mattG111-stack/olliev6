"""Let the model write its own read-only SQL, safely.

Seven hand-written tools can only answer the seven questions we thought of.
"Which suburb has the most 4-bed houses under $1.2M with land over 700m2" is a
perfectly reasonable question that no curated tool covers. Giving the model the
schema and letting it query is the only way to answer arbitrary questions.

That means the safety has to be real, not a keyword blocklist. Four layers:

1. READ ONLY TRANSACTION — Postgres itself rejects any write. Verified: an
   UPDATE inside one raises InternalError. This is the load-bearing control;
   string matching alone would be a sieve.
2. Table allowlist — a read-only transaction still permits `SELECT * FROM users`,
   which holds password hashes. Only the property tables are reachable.
3. statement_timeout — a runaway cross join can't wedge the connection pool.
4. Row cap — a LIMIT is appended when absent, so nothing returns 130k rows.

Anything rejected comes back to the model as an error string, so it can correct
its own query rather than the request failing.
"""

from __future__ import annotations

import json
import re

from sqlalchemy import text

from db import engine

STATEMENT_TIMEOUT_MS = 10_000
MAX_ROWS = 200

# Everything that describes a property, a market or where the data came from.
#
# This used to be four tables, and three of those four were the only ones the
# schema below described — so in practice Ollie could see live listings and
# sales and nothing else. Rent was reachable but undocumented, which is the
# same as unreachable: the model cannot query a table it has never been told
# exists. Ask it what a three-bedroom in Glenfield rents for and it answered
# from properties_for_sale.est_weekly_rent, our own estimate, rather than from
# the actual rental listings sitting in the next table.
#
# The line is not "property data vs the rest". It is: does this table describe
# HOUSES AND THE MARKET, or does it describe PEOPLE AND SECRETS.
#
# Out, and staying out:
#   users, verification_codes  — password hashes and live login codes
#   app_settings               — encrypted API tokens, including Apify's
#   promoters, referrals, referral_clicks, promo_assets, commissions
#                              — customer names, emails and money
#   assistant_logs             — what other people asked, in their own words
#   wish_lists                 — one person's saved searches, and query_data has
#                                no notion of whose session is asking, so
#                                allowing it would let any customer read every
#                                other customer's list
#   bug_reports                — free text, so anything a reporter pasted in
#
# A read-only transaction does not help with any of those: SELECT is exactly
# the dangerous verb. The allowlist is what keeps them out, so it is written
# as an allowlist and never as a blocklist.
ALLOWED_TABLES = {
    # the market
    "properties_for_sale",
    "properties_sold",
    "properties_rent",
    # the land under it
    "parcel_cache",
    # where the data came from and how fresh it is
    "import_batches",
    # what people do with it — counts only, no names anywhere in these
}

# Matches a table name after FROM / JOIN, with optional schema qualifier.
_TABLE_REF = re.compile(
    r"\b(?:from|join)\s+(?:only\s+)?([a-zA-Z_][\w$]*(?:\.[a-zA-Z_][\w$]*)?)",
    re.IGNORECASE,
)
_LIMIT = re.compile(r"\blimit\s+\d+", re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    """Comments can hide a second statement or a blocked identifier."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


class UnsafeQuery(ValueError):
    """The query was rejected before it reached the database."""


def validate(sql: str) -> str:
    """Reject anything that isn't a single, read-only, allowlisted SELECT."""
    cleaned = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        raise UnsafeQuery("Empty query.")

    # One statement only — a trailing semicolon is fine, an embedded one is not.
    if ";" in cleaned:
        raise UnsafeQuery("Only one statement per query. Remove the semicolon.")

    identifiers = re.sub(r"'(?:''|[^'])*'", "''", cleaned)
    identifiers = re.sub(r'"([a-zA-Z_][\w$]*)"', r'\1', identifiers)
    # Explicit JOINs make every table reference subject to the allowlist.
    # Reject comma joins rather than silently ignoring their second table.
    depth, from_depths = 0, set()
    for token in re.findall(r"[a-zA-Z_][\w$]*|[(),]", identifiers):
        upper = token.upper()
        if token == "(": depth += 1
        elif token == ")":
            from_depths.discard(depth)
            depth -= 1
        elif upper == "FROM": from_depths.add(depth)
        elif upper in {"WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "UNION", "EXCEPT", "INTERSECT"}:
            from_depths.discard(depth)
        elif token == "," and depth in from_depths:
            raise UnsafeQuery("Use explicit JOINs rather than comma-separated tables.")
    lowered = cleaned.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise UnsafeQuery("Only SELECT (or WITH ... SELECT) queries are allowed.")

    # CTE names are legitimate FROM targets; collect them so they aren't
    # mistaken for real tables.
    cte_names = {
        m.group(1).lower()
        for m in re.finditer(r"\b([a-zA-Z_][\w$]*)\s+as\s*\(", identifiers, re.IGNORECASE)
    }

    referenced = {m.group(1).lower() for m in _TABLE_REF.finditer(identifiers)}
    for ref in referenced:
        bare = ref.split(".")[-1]
        if "." not in ref and ref in cte_names:
            continue
        if bare not in ALLOWED_TABLES:
            raise UnsafeQuery(
                f"Table '{ref}' is not queryable. Available tables: "
                + ", ".join(sorted(ALLOWED_TABLES))
            )

    if not _LIMIT.search(cleaned):
        cleaned = f"{cleaned} LIMIT {MAX_ROWS}"
    return cleaned


def scope_live_listings(sql: str, dialect=None) -> str:
    """Enforce approved data before aggregation, regardless of generated SQL."""
    identifiers = re.sub(r"'(?:''|[^'])*'", "''", sql)
    tables = [t for t in ("properties_for_sale", "properties_sold", "properties_rent", "import_batches")
              if re.search(r"\b" + t + r"\b", identifiers, re.I)]
    if not tables:
        return sql
    if re.match(r"\s*WITH\s+RECURSIVE\b", identifiers, re.I):
        raise UnsafeQuery("Use a non-recursive query for approved records.")
    for table in tables:
        if re.search(r'[\w\"]\s*\.\s*\"?' + table + r'\b', identifiers, re.I):
            raise UnsafeQuery(f"Use the unqualified {table} table for approved records.")
        if re.search(r'\b' + table + r'\"?\s*(?:\([^)]*\)\s*)?AS\s*\(', identifiers, re.I):
            raise UnsafeQuery(f"{table} is reserved for approved records; choose another CTE name.")
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql
    from models import ImportBatch, PropertyForSale
    from routers.properties import _hide_bad_data
    dialect = dialect or postgresql.dialect()
    schema = "main" if dialect.name == "sqlite" else "public"
    bodies = []
    for table in tables:
        if table == "properties_for_sale":
            visible = _hide_bad_data(select(text("properties_for_sale.*")).select_from(PropertyForSale)).where(
                PropertyForSale.import_batch_id.in_(select(ImportBatch.id).where(
                    ImportBatch.is_active.is_(True), ImportBatch.batch_type == "for_sale",
                    ImportBatch.status == "published")))
            body = str(visible.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
            body = body.replace("FROM properties_for_sale", f"FROM {schema}.properties_for_sale")
            body = body.replace("FROM import_batches", f"FROM {schema}.import_batches")
        elif table == "import_batches":
            body = f"SELECT * FROM {schema}.import_batches WHERE status IN ('published', 'archived')"
        else:
            kind = "sold" if table == "properties_sold" else "rent"
            active = "" if kind == "sold" else " AND is_active = true"
            body = (f"SELECT * FROM {schema}.{table} WHERE import_batch_id IN "
                    f"(SELECT id FROM {schema}.import_batches WHERE batch_type = '{kind}' "
                    f"AND status = 'published'{active})")
        bodies.append(f"{table} AS ({body})")
    prefix = "WITH " + ", ".join(bodies)
    if re.match(r"\s*WITH\b", sql, re.I):
        return prefix + ", " + re.sub(r"^\s*WITH\s+", "", sql, count=1, flags=re.I)
    return prefix + " " + sql


def run(sql: str) -> str:
    """Validate then execute inside a read-only, time-limited transaction."""
    try:
        safe = scope_live_listings(validate(sql))
    except UnsafeQuery as exc:
        return f"Query rejected: {exc}"

    try:
        with engine.connect() as conn:
            # Both statements must be inside the same transaction as the query.
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            result = conn.execute(text(safe))
            cols = list(result.keys())
            rows = [dict(zip(cols, r)) for r in result.fetchmany(MAX_ROWS)]
    except Exception as exc:  # noqa: BLE001 - returned to the model to self-correct
        return f"Query failed: {type(exc).__name__}: {str(exc)[:400]}"

    return json.dumps(
        {
            "sql": safe,
            "row_count": len(rows),
            "truncated": len(rows) >= MAX_ROWS,
            "rows": rows,
        },
        default=str,
    )


def distinct_values(table: str, column: str, limit: int = 40) -> str:
    """Return the actual distinct values of a column, with counts.

    The single biggest cause of a wrong answer is the model guessing a value
    that isn't spelled the way the data stores it — 'Flatbush' vs 'Flat Bush',
    English vs Chinese property_type. This lets it check first.
    """
    if table not in ALLOWED_TABLES:
        return f"Table '{table}' is not queryable."
    if not re.fullmatch(r"[a-zA-Z_][\w]*", column or ""):
        return "Invalid column name."
    # Live/rental imports are snapshots; sold imports accumulate history.
    # Match ingest.sold_batch_ids, including its accepted batch statuses.
    if table == "properties_sold":
        batch = (
            "import_batch_id IN (SELECT id FROM import_batches "
            "WHERE batch_type = 'sold' AND status = 'published')"
        )
    elif table in ("properties_for_sale", "properties_rent"):
        kind = "rent" if table == "properties_rent" else "for_sale"
        batch = (
            "import_batch_id IN (SELECT id FROM import_batches "
            f"WHERE status = 'published' AND is_active AND batch_type = '{kind}')"
        )
    else:
        batch = "TRUE"
    sql = (
        f"SELECT {column} AS value, COUNT(*) AS n FROM {table} "
        f"WHERE {batch} AND {column} IS NOT NULL GROUP BY {column} "
        f"ORDER BY n DESC LIMIT {min(int(limit), 100)}"
    )
    return run(sql)


# --- schema description handed to the model -------------------------------

SCHEMA = """You can query these Postgres tables directly with read-only SQL.

DATASET SCOPE — imports have different meanings:
- For-sale and rental imports are snapshots. Use current batches:
    WHERE import_batch_id IN (SELECT id FROM import_batches
                              WHERE status = 'published' AND is_active AND batch_type = 'for_sale')
  Use batch_type = 'rent' for properties_rent. Never mix old rental snapshots.
- Sold imports accumulate transaction history. Do NOT restrict sales to the
  newest or active batch; that would discard earlier sales used by valuations:
    WHERE import_batch_id IN (SELECT id FROM import_batches
      WHERE batch_type = 'sold' AND status = 'published')
  Only published deliveries are approved for customer answers. Staged data is private.
- Apply the requested region/suburb and date range as additional filters.
  Counts are records, not necessarily unique properties or transactions.

properties_for_sale — current VISIBLE for-sale listings, automatically scoped
  to the active batch and the same visibility rules as the property pages.
  Held, delisted, sold, placeholder-price and invalid-margin records are excluded
  before filtering or counting. Use this table without a schema qualifier.
  Historical or held listing rows are not available through this table.
  Counts are listing records, not unique homes: deduplicate by normalized full
  address AND suburb when asked for homes, preserving unit numbers.
  id, address, suburb, district, region, postcode, latitude, longitude
  property_type, type_of_title, zoning, land_slope_contour
  beds, baths, cars, floor_area_m2, land_area_m2, building_age
  sale_method, price_display, deal_block_reason
  asking_price            what it is listed at
  cv_numeric              council valuation
  land_value_numeric, improvement_value_numeric
  fair_value              OUR valuation (the sold-data estimate)
  buy_price               what we'd pay
  margin                  (fair_value - asking_price) / asking_price, as a fraction
  pred_vs_cv              our value vs CV, as a fraction
  confidence              'low' | 'medium' | 'high'
  comps_used              how many sold comps backed the valuation
  range_low, range_high   likely sale range
  predicted_days, days_on_market
  est_weekly_rent, est_gross_yield, annual_cashflow, cash_on_cash
  breakeven_deposit_pct
  is_underpriced, is_cashflow_positive, is_subdividable   booleans
  max_addl_lots, min_lot_m2, total_subdivided_value, best_net_gain, best_strategy
  opportunity_score_pct
  has_swimming_pool, is_new_construction, is_coastal_waterfront
  url, image_url, image_urls, import_batch_id

properties_sold — completed sales, the evidence base
  id, address, suburb, district, property_type, type_of_title
  beds, baths, floor_area_m2, land_area_m2
  sale_price, cv_numeric, land_value_numeric
  sale_method   'A - Auction' | 'P - Private Treaty(Neg.)' | 'T - Tender'
  sold_date, days_on_market, has_swimming_pool, import_batch_id

properties_rent — actual rental listings. THE source for any rent question.
  id, address, suburb, district, property_type, beds, baths
  floor_area_m2, land_area_m2
  weekly_rent             what it is advertised at, per week
  listing_date_rent, import_batch_id
  Do NOT answer a rent question from properties_for_sale.est_weekly_rent —
  that is our own estimate for a house that is for sale, not a rental. Use
  this table, and say how many listings the figure came from.

parcel_cache — legal parcel boundaries from LINZ, one per looked-up point.
  id, lat_key, lng_key, status ('linz' | 'none'), area_m2, appellation, ring
  area_m2 is the SURVEYED land area and beats a listing's stated land area
  where they disagree. status='none' means LINZ has no parcel there.

import_batches — one row per uploaded file.
  id, batch_type ('for_sale' | 'sold' | 'rent'), region, filename, created_at
  is_active, status ('published' | 'archived')
  rows_total, rows_inserted, rows_rejected
  note          WHY rows were rejected, in words and biggest first — e.g.
                "rejected — 4,200 apartments (deliberately excluded); 3,100 no
                council valuation, which every valuation method needs". This is
                the answer to "why did a load reject so many": read it out
                rather than guessing from the counts.

"""
