"""Trade Me's Auckland sales export — used to fill holes, not to add rows.

The export is a large, clean list of settled sales: 59,445 of them across 364
suburbs in the sample, each carrying a price, a council valuation, a floor area,
coordinates and an ownership type. Our own records have gaps in every one of
those fields, and this file can close them.

What it is NOT is a second opinion on value.

    Their est_value against the actual sale price, 54,692 sales:

        sold in the last 30 days   median error  +0.4%   median absolute  1.3%
        sold during 2025                         -1.2%                    2.0%
        sold during 2024                         -2.4%                    3.3%

    No valuation model is accurate to 1.3%; ours runs near 9% and that is
    competitive. The month-by-month curve gives it away — -2.2% for sales in
    August 2024, sliding smoothly to 0.0% for sales happening now, with every
    estimate stamped the same date and 99.4% of them landing on a round $5,000.

    They take the sale price, index it forward to today, and publish that.

So their figure is stored and displayed as "Trade Me says", because it is what
Trade Me shows the public and a reader may want to see it. It is never fed into
a valuation, a margin, a deal flag or a comparison — doing so would confirm our
numbers against a sale price we already hold and call the circle accuracy.

Rows are matched to ours on ADDRESS, and only ever fill a field we are missing.
Nothing we hold is overwritten: our own export carries bedrooms, bathrooms, days
on market and sale method, none of which this file has, and a field we already
have is one this file cannot improve.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy.orm import Session

from .ingest import canonical_sale_date
from .models import PropertyForSale, PropertySold

# Their sale dates are M/D/YYYY — 9/14/2024, 4/30/2026. Read day-first they
# transpose silently for every date whose day is 12 or lower, which is about
# two rows in five, and a sale moves months without anything failing.
_MONTH_FIRST = True

_MONEY = re.compile(r"([\d.,]+)\s*([KkMm])?")

# What each of their columns is called in our schema. Only the fields we can
# actually use — their estimate is handled separately and deliberately.
COLUMNS = {
    "sale_price": "sale_price",
    "sale_date": "sold_date",
    "floor_area_m2": "floor_area_m2",
    "land_area_m2": "land_area_m2",
    "capital_value": "cv_numeric",
    "land_value": "land_value_numeric",
    "improvement_value": "improvement_value_numeric",
    "latitude": "latitude",
    "longitude": "longitude",
    "ownership_type": "type_of_title",
    "property_type": "property_type",
    "cover_image_url": "image_url",
}

# Fields filled only when we have nothing. Ordered as a reader would read them.
FILLABLE = ("floor_area_m2", "land_area_m2", "cv_numeric", "land_value_numeric",
            "improvement_value_numeric", "latitude", "longitude",
            "type_of_title", "property_type", "image_url")


def money(v) -> float | None:
    """"$815K" / "$1.23M" / 820000.0 → a number."""
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _MONEY.search(str(v).replace("$", "").strip())
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    return n * 1_000_000 if unit == "m" else n * 1_000 if unit == "k" else n


_UNIT = re.compile(r"^(\d+)\s*[/\\]\s*")
_PUNCT = re.compile(r"[^\w/\s]")

# Street types, written out. Every source abbreviates differently — our export
# says "Donovan Street", a portal says "Donovan St", and as bare text those are
# two different houses. Matching on the long form makes them one.
_STREET_TYPES = {
    "st": "street", "str": "street", "rd": "road", "ave": "avenue",
    "av": "avenue", "dr": "drive", "drv": "drive", "pl": "place",
    "cres": "crescent", "cr": "crescent", "tce": "terrace", "ter": "terrace",
    "ln": "lane", "cl": "close", "ct": "court", "crt": "court",
    "gr": "grove", "grv": "grove", "pde": "parade", "prd": "parade",
    "hwy": "highway", "sq": "square", "blvd": "boulevard", "bvd": "boulevard",
    "hts": "heights", "esp": "esplanade", "qy": "quay", "mt": "mount",
}
# Only the LAST word is a street type. "St Heliers Road" starts with a saint,
# and "Mount Street" is not "mount street" the suburb — rewriting either would
# invent an address rather than normalise one.
_ABBREV = re.compile(r"\b(" + "|".join(sorted(_STREET_TYPES, key=len, reverse=True))
                     + r")\.?$")


def _expand_street_type(street: str) -> str:
    return _ABBREV.sub(lambda m: _STREET_TYPES[m.group(1)], street)


def address_key(address, suburb=None) -> str | None:
    """A street address reduced to something two sources can agree on.

    Trade Me writes "1 Abernethy Way, Patumahoe, Pukekohe"; our export writes
    "3/107 Donovan Street, Blockhouse Bay, Auckland City, Auckland". Everything
    after the street is a different number of location parts in each, so only
    the street itself is compared, with the suburb alongside it to keep two
    "12 Queen Street"s in different suburbs apart.
    """
    if not address:
        return None
    street = str(address).split(",")[0].strip().lower()
    street = _PUNCT.sub(" ", street)
    street = re.sub(r"\s+", " ", street).strip()
    if not street:
        return None
    # "3 / 107 donovan street" and "3/107 donovan street" are one address.
    street = _UNIT.sub(r"\1/", street.replace(" / ", "/"))
    # "donovan st" and "donovan street" are one address too.
    street = _expand_street_type(street)
    sub = re.sub(r"\s+", " ", str(suburb or "").strip().lower())
    return f"{street}|{sub}"


def load(csv) -> pd.DataFrame:
    """Their export, with the columns renamed and the money and dates parsed.

    Raises ValueError naming what is missing when handed something else. An
    admin who picks the wrong file out of a downloads folder — easily done, they
    are all CSVs of Auckland property — needs to be told which file they picked,
    not "ValueError".
    """
    df = csv if isinstance(csv, pd.DataFrame) else pd.read_csv(csv, low_memory=False)

    # address is the only column without which nothing can be matched at all.
    # The other two are what makes it THEIR export rather than some other list.
    missing = [c for c in ("address", "sale_date", "est_value") if c not in df.columns]
    if "address" in missing or len(missing) == 3:
        found = ", ".join(list(df.columns)[:8]) or "no columns at all"
        raise ValueError(
            "This does not look like a Trade Me sales export — it has no "
            f"{missing[0]} column. The columns it does have are: {found}")

    out = df.rename(columns=COLUMNS).copy()

    for c in ("sale_price", "cv_numeric", "land_value_numeric",
              "improvement_value_numeric", "floor_area_m2", "land_area_m2",
              "latitude", "longitude"):
        if c in out.columns:
            out[c] = out[c].map(money)

    if "sold_date" in out.columns:
        out["sold_date"] = out["sold_date"].map(
            lambda v: canonical_sale_date(v, month_first=_MONTH_FIRST))

    # Their own figure, kept apart from everything else — see the module note.
    for src, dst in (("est_value", "tm_valuation"),
                     ("est_value_low", "tm_valuation_low"),
                     ("est_value_high", "tm_valuation_high")):
        out[dst] = df[src].map(money) if src in df.columns else None
    out["tm_valuation_date"] = (df["est_value_date"].astype(str)
                                if "est_value_date" in df.columns else None)

    # A type they are only "medium" confident about is a guess, and a guess in
    # this column silently moves a property into a different comparable set.
    if "property_type_confidence" in df.columns and "property_type" in out.columns:
        out.loc[df["property_type_confidence"].astype(str).str.lower() != "high",
                "property_type"] = None

    out["_key"] = [address_key(a, s) for a, s
                   in zip(out.get("address", []), out.get("suburb", []))]
    return out[out["_key"].notna()]


@dataclass
class FillResult:
    """What one upload actually changed, field by field."""
    rows_seen: int = 0            # rows in their file
    matched: int = 0              # our properties an address matched
    unmatched: int = 0            # their rows we hold nothing for
    filled: dict[str, int] = field(default_factory=dict)
    valuations: int = 0           # "Trade Me says" figures stored
    conflicts: list[str] = field(default_factory=list)

    @property
    def note(self) -> str:
        bits = [f"matched {self.matched:,} of {self.rows_seen:,}"]
        if self.valuations:
            bits.append(f"{self.valuations:,} Trade Me figures")
        for k, v in sorted(self.filled.items(), key=lambda kv: -kv[1]):
            bits.append(f"{v:,} {k.replace('_', ' ')}")
        return " · ".join(bits)


# Two records of one sale, dated differently by two sources. Beyond this they
# are two different sales of the same house.
_SAME_SALE_DAYS = 60


def _days_apart(a, b) -> int | None:
    """How far apart two dates are, or None if that is not a meaningful question."""
    if not a or not b:
        return None
    try:
        d = abs((pd.to_datetime(str(a)[:10]) - pd.to_datetime(str(b)[:10])).days)
    except (ValueError, TypeError):
        return None
    return d if 0 < d <= _SAME_SALE_DAYS else None


def _set_valuation(p, theirs, dry_run: bool) -> None:
    """Store their figure for display. Refreshed on every upload rather than
    filled once, because it moves with their index."""
    if dry_run:
        return
    p.tm_valuation = float(theirs["tm_valuation"])
    p.tm_valuation_low = theirs.get("tm_valuation_low")
    p.tm_valuation_high = theirs.get("tm_valuation_high")
    p.tm_valuation_date = theirs.get("tm_valuation_date")


def _is_missing(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and v != v:
        return True
    return isinstance(v, str) and not v.strip()


def fill(db: Session, frame: pd.DataFrame, *, region: str = "Auckland",
         dry_run: bool = False) -> FillResult:
    """Fill our gaps from their file. Never overwrites a value we already hold.

    Runs over both live listings and sold records: a property they have sold is
    frequently one we hold a sold record for with no floor area, and just as
    often one that is back on the market today.
    """
    res = FillResult(rows_seen=len(frame))
    by_key: dict[str, pd.Series] = {}
    for _, row in frame.iterrows():
        by_key.setdefault(row["_key"], row)      # first sighting wins
    if not by_key:
        return res

    matched_keys: set[str] = set()
    # address key -> the most recent sold row we hold for it, and their row.
    newest: dict[str, tuple] = {}
    for model in (PropertyForSale, PropertySold):
        # Streamed rather than loaded at once. Production holds six figures of
        # sold records and .all() builds every one of them as an object before
        # the first is looked at — a large, pointless spike on a small dyno for
        # a job that touches a few hundred of them.
        rows = db.query(model).filter(model.region == region).yield_per(1_000)
        for p in rows:
            key = address_key(p.address, p.suburb)
            theirs = by_key.get(key) if key else None
            if theirs is None:
                continue
            matched_keys.add(key)
            res.matched += 1

            for f in FILLABLE:
                if f not in theirs or not hasattr(p, f):
                    continue
                new = theirs[f]
                if _is_missing(new) or not _is_missing(getattr(p, f)):
                    continue
                if not dry_run:
                    setattr(p, f, new)
                res.filled[f] = res.filled.get(f, 0) + 1

            # A sold record with no price is not a comparable sale. Theirs is
            # the same sale, so it can supply one — but only if ours is silent.
            if isinstance(p, PropertySold):
                if _is_missing(p.sale_price) and not _is_missing(theirs.get("sale_price")):
                    if not dry_run:
                        p.sale_price = float(theirs["sale_price"])
                    res.filled["sale_price"] = res.filled.get("sale_price", 0) + 1
                if _is_missing(p.sold_date) and not _is_missing(theirs.get("sold_date")):
                    if not dry_run:
                        p.sold_date = theirs["sold_date"]
                    res.filled["sold_date"] = res.filled.get("sold_date", 0) + 1
                # The two sources date the SAME sale differently — theirs looks
                # like the agreement, ours the settlement, and they sit days to
                # weeks apart. Recorded so the difference is visible rather than
                # discovered later as two sales of one house. Anything further
                # apart than _SAME_SALE_DAYS is a different transaction, not a
                # disagreement about one.
                elif _days_apart(theirs.get("sold_date"), p.sold_date) is not None:
                    if len(res.conflicts) < 50:
                        res.conflicts.append(
                            f"{p.address}: ours {str(p.sold_date)[:10]}, "
                            f"theirs {theirs['sold_date'][:10]}")

            # Their figure is a CURRENT one — what they say the property is
            # worth today. On a live listing that is exactly what to show. On a
            # sold record it belongs only to the most recent sale: attached to
            # every row it would print "Trade Me says $1.57M" beside a sale from
            # 1999, which is three different mistakes at once.
            if not _is_missing(theirs.get("tm_valuation")):
                if isinstance(p, PropertySold):
                    prev = newest.get(key)
                    if prev is None or str(p.sold_date or "") > str(prev[1] or ""):
                        newest[key] = (p, p.sold_date, theirs)
                else:
                    _set_valuation(p, theirs, dry_run)
                    res.valuations += 1

    for p, _when, theirs in newest.values():
        _set_valuation(p, theirs, dry_run)
        res.valuations += 1

    res.unmatched = len(by_key) - len(matched_keys)
    if not dry_run:
        db.commit()
    return res
