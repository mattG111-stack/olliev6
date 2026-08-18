"""Calendar-month helpers shared by the dashboards and the suburb panel.

Lifted out of routers/dashboards.py because routers/properties.py needs them
too, and dashboards already imports from properties — importing back the other
way is a cycle. A leaf module both can depend on is the fix.
"""
from __future__ import annotations


def sold_year_month(sold_date: str | None) -> tuple[int, int] | None:
    """(year, month) from a stored sale date, in either format we hold.

    Two spellings are in the database at once and will be for as long as it
    takes older rows to be rewritten:

        '5/14/2026'   M/D/YYYY, what the original scraper wrote as text
        '2026-05-14'  ISO, what ingest canonicalises to now

    Everything downstream — the month buckets, the comparables recency filter,
    the buy-price engine's window — used to parse only the first. Reading ISO
    with a slash-splitter does not raise; it just quietly returns nothing, and
    each caller then failed a different way: no comps, empty trends, or a
    recency bound skipped altogether so 1993 sales priced today's houses.
    One parser, so a new format can only ever be wrong in one place.
    """
    if not sold_date:
        return None
    s = str(sold_date).strip()
    if not s:
        return None
    if "-" in s[:5]:                       # ISO: YYYY-MM-DD, possibly with a time
        head = s.split(" ")[0].split("T")[0]
        bits = head.split("-")
        if len(bits) < 2:
            return None
        try:
            y, m = int(bits[0]), int(bits[1])
        except ValueError:
            return None
    else:                                  # legacy M/D/YYYY
        parts = s.split("/")
        if len(parts) != 3:
            return None
        try:
            m, y = int(parts[0]), int(parts[2])
        except ValueError:
            return None
    if not (1 <= m <= 12 and 1900 < y < 2100):
        return None
    return y, m


def recent_sales(rows, years: int, *, attr: str = "sold_date") -> list:
    """Sales within `years` of the most recent one present. Order is preserved.

    Sold data used to arrive one recent batch at a time, so anything reading it
    could treat every row as current. It now carries decades of history, and a
    price is only evidence about the moment it was struck: mixing 1994 sales
    into a 2026 median moves it, and feeding them to a hedonic fit destroys it
    outright — the same house appears repeatedly with identical rooms, floor and
    land against wildly different prices, so every coefficient's interval
    swallows zero and the honest answer becomes "no measurable effect".

    Measured from the newest sale in the DATA, not from today, so a dataset
    loaded late does not empty itself. Rows with an unreadable date are kept:
    dropping them would discard everything from a file with no usable dates.
    """
    dated = [(r, sold_year_month(getattr(r, attr, None))) for r in rows]
    months = [ym[0] * 12 + ym[1] for _, ym in dated if ym]
    if not months:
        return list(rows)
    cutoff = max(months) - years * 12
    return [r for r, ym in dated if ym is None or (ym[0] * 12 + ym[1]) >= cutoff]


def _period(sold_date: str | None) -> str | None:
    """'5/14/2026' or '2026-05-14' -> '2026-05'."""
    ym = sold_year_month(sold_date)
    return f"{ym[0]:04d}-{ym[1]:02d}" if ym else None


def _shift_period(period: str, back_months: int) -> str:
    """'2026-06' shifted back 17 months -> '2025-01'. Keeps the YYYY-MM ordering."""
    y, m = int(period[:4]), int(period[5:7])
    total = y * 12 + (m - 1) - back_months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _months_range(start: str, end: str) -> list[str]:
    """Every calendar month from start to end inclusive, as YYYY-MM. Emitting the
    full run (not just months with sales) keeps the x-axis time-proportional, so a
    year with no sales reads as a year-wide gap instead of one compressed step."""
    s = int(start[:4]) * 12 + int(start[5:7]) - 1
    e = int(end[:4]) * 12 + int(end[5:7]) - 1
    return [f"{t // 12:04d}-{t % 12 + 1:02d}" for t in range(s, e + 1)]
