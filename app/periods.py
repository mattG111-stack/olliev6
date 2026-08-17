"""Calendar-month helpers shared by the dashboards and the suburb panel.

Lifted out of routers/dashboards.py because routers/properties.py needs them
too, and dashboards already imports from properties — importing back the other
way is a cycle. A leaf module both can depend on is the fix.
"""
from __future__ import annotations


def _period(sold_date: str | None) -> str | None:
    """'5/14/2026' -> '2026-05'. The scraper stores M/D/YYYY as text."""
    if not sold_date:
        return None
    parts = str(sold_date).strip().split("/")
    if len(parts) != 3:
        return None
    try:
        m, y = int(parts[0]), int(parts[2])
    except ValueError:
        return None
    if not (1 <= m <= 12 and 1900 < y < 2100):
        return None
    return f"{y:04d}-{m:02d}"


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
