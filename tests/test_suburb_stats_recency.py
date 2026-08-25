"""Suburb figures describe today's market, not every sale ever recorded.

A sale price is evidence about the moment it was struck. That was implicit while
sold files covered a recent window — every row was current, so nothing had to
check. Multi-year history breaks it, and the room fit breaks loudest: the same
house recurs with identical bedrooms, bathrooms, floor and land against 1994 and
2026 prices, so the regression sees one set of inputs mapping to wildly
different outputs, every coefficient's interval swallows zero, and the page
reports "no measurable effect here" for a suburb where a bedroom is plainly
worth something.

The medians fail more quietly, which is worse: a median sale price computed over
thirty years of sales is simply wrong, and looks entirely reasonable.
"""
from __future__ import annotations

import random

import pytest

from app.periods import recent_sales
from app.pricing import assumptions as A
from app.routers.properties import _fit_rooms


class _Sale:
    """Just the attributes the fit and the recency filter read."""

    def __init__(self, price, floor, land, beds, baths, sold_date):
        self.sale_price = price
        self.floor_area_m2 = floor
        self.land_area_m2 = land
        self.beds = beds
        self.baths = baths
        self.sold_date = sold_date
        self.days_on_market = 30
        self.property_type = "House"


def _suburb(n=120, seed=7):
    """Recent sales where a bedroom really is worth ~$80k, plus their history."""
    rnd = random.Random(seed)
    recent, history = [], []
    for _ in range(n):
        beds = rnd.choice([2, 3, 4, 5])
        baths = rnd.choice([1, 2, 3])
        floor = 60 + beds * 30 + rnd.gauss(0, 8)
        land = 300 + rnd.gauss(0, 60)
        price = (300_000 + floor * 4_000 + land * 400
                 + beds * 80_000 + baths * 45_000 + rnd.gauss(0, 40_000))
        recent.append(_Sale(price, floor, land, beds, baths, "2026-01-15"))
        # The same houses, sold before, at the price levels of those years —
        # carrying TODAY's room counts, because that is all the export knows.
        for year, level in ((1994, 0.14), (2002, 0.24), (2012, 0.45), (2019, 0.72)):
            history.append(_Sale(price * level, floor, land, beds, baths,
                                 f"{year}-06-01"))
    return recent, history


def test_full_history_destroys_the_room_fit():
    """The failure this guards against — kept so the fix cannot be undone quietly."""
    recent, history = _suburb()
    fits = _fit_rooms(recent + history)
    assert fits["beds"].dollars is None
    # Asserting the meaning, not the wording: the note now also reports how many
    # sales it looked at and whether property types had to be pooled, and a test
    # pinned to an exact sentence turns every improvement to that message into a
    # failure that says nothing about the behaviour.
    assert "no measurable effect" in (fits["beds"].note or "")


def test_recency_filter_restores_it():
    recent, history = _suburb()
    kept = recent_sales(recent + history, A.COMP_MAX_AGE_YEARS)
    fits = _fit_rooms(kept)

    assert len(kept) == len(recent), "the filter kept sales outside the window"
    assert fits["beds"].dollars is not None, "a bedroom still measures as nothing"
    # Generated so a bedroom is worth ~$80k; the fit rounds to $5k.
    assert 60_000 <= fits["beds"].dollars <= 100_000
    assert 25_000 <= fits["baths"].dollars <= 65_000


def test_recency_is_measured_from_the_data_not_today():
    """A dataset loaded months late must not filter itself away."""
    rows = [_Sale(900_000, 120, 500, 3, 1, f"20{y:02d}-06-01") for y in (10, 15, 18, 19, 20)]
    kept = recent_sales(rows, 3)
    assert {r.sold_date for r in kept} == {"2018-06-01", "2019-06-01", "2020-06-01"}


def test_rows_with_unreadable_dates_are_kept():
    """Better one sale of unknown age than discarding a file with no dates."""
    rows = [_Sale(900_000, 120, 500, 3, 1, d)
            for d in ("2026-01-01", "not a date", None, "1994-01-01")]
    kept = recent_sales(rows, 3)
    dates = [r.sold_date for r in kept]
    assert "not a date" in dates and None in dates
    assert "1994-01-01" not in dates


def test_both_date_formats_filter_together():
    """Legacy M/D/YYYY rows and ISO rows must age out on the same clock."""
    rows = [_Sale(900_000, 120, 500, 3, 1, d)
            for d in ("2026-01-15", "6/1/2025", "6/1/2010", "2010-06-01")]
    kept = {r.sold_date for r in recent_sales(rows, 3)}
    assert kept == {"2026-01-15", "6/1/2025"}
