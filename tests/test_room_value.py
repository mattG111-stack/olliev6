"""What another bedroom is worth: a 3-bed's price against a 4-bed's.

The panel used to fit price ~ floor + land + beds + baths and report the beds
coefficient. That regression holds FLOOR AREA CONSTANT, so what it actually
answers is "what if this same house were divided into more rooms" — a question
nobody asked, and one whose honest answer is often negative. It reported minus
$210,000 a bedroom in production, next to a bathroom at plus $300,000.

A four-bedroom house is bigger than a three-bedroom one. That extra size is not
a confounder to be controlled away; it is the thing being bought.

Bathrooms do need a control, and it is the same idea one level down: compare
1-bath against 2-bath WITHIN one bedroom count. Left alone, 1-bath homes are
simply the small ones, and the comparison measures bedrooms again — it returned
$524k in testing, more than the bedroom it was supposedly independent of.
"""
from __future__ import annotations

import random

import pytest

from app.routers.properties import ROOM_CELL_MIN, _room_effect_by_cell


class _Sale:
    def __init__(self, price, beds, baths, floor=120.0, land=400.0):
        self.sale_price = price
        self.beds = beds
        self.baths = baths
        self.floor_area_m2 = floor
        self.land_area_m2 = land
        self.property_type = "House"
        self.days_on_market = 30


def _suburb(seed=9):
    """Bedroom genuinely worth ~$400k; a 2nd bathroom in a 3-bed worth ~$90k."""
    rnd = random.Random(seed)
    rows = []
    for beds, base, n in [(2, 1_150_000, 14), (4, 1_850_000, 19), (5, 2_350_000, 8)]:
        for _ in range(n):
            rows.append(_Sale(base + rnd.gauss(0, 180_000), beds,
                              1 if beds <= 2 else 2, floor=60 + beds * 30))
    for _ in range(16):
        rows.append(_Sale(1_450_000 + rnd.gauss(0, 150_000), 3, 1, floor=150))
    for _ in range(14):
        rows.append(_Sale(1_540_000 + rnd.gauss(0, 150_000), 3, 2, floor=150))
    return rows


def test_a_bedroom_is_worth_roughly_what_the_sales_say():
    fit = _room_effect_by_cell(_suburb(), "beds")
    assert fit.dollars is not None, fit.note
    # True value ~$400k; sample noise is wide, so this is a sanity band.
    assert 200_000 < fit.dollars < 600_000, f"got {fit.dollars} ({fit.note})"


def test_a_bedroom_is_never_reported_as_destroying_value():
    """The reported bug: minus $210,000 a bedroom.

    In a market where bigger homes sell for more, this figure cannot be negative
    without something being deeply wrong with the question being asked.
    """
    fit = _room_effect_by_cell(_suburb(), "beds")
    assert fit.dollars is not None and fit.dollars > 0, (
        f"a bedroom was valued at {fit.dollars} ({fit.note})"
    )


def test_the_bathroom_figure_is_not_just_the_bedroom_again():
    """Held within one bedroom count, so it cannot smuggle in the extra size."""
    beds = _room_effect_by_cell(_suburb(), "beds")
    baths = _room_effect_by_cell(_suburb(), "baths")
    assert baths.dollars is not None, baths.note
    assert baths.dollars < beds.dollars, (
        f"a bathroom ({baths.dollars}) came out worth more than a bedroom "
        f"({beds.dollars}) — the comparison is measuring house size"
    )
    assert "bedroom homes" in (baths.note or ""), (
        "the bathroom comparison did not state which bedroom count it held"
    )


def test_it_says_which_cells_it_compared():
    """A number with no stated basis cannot be argued with."""
    fit = _room_effect_by_cell(_suburb(), "beds")
    assert "beds" in (fit.note or "") and "sales" in (fit.note or ""), fit.note


def test_a_thin_suburb_declines_to_answer():
    rows = [_Sale(1_400_000, 3, 1) for _ in range(3)] + [_Sale(1_800_000, 4, 2) for _ in range(2)]
    fit = _room_effect_by_cell(rows, "beds")
    assert fit.dollars is None
    assert str(ROOM_CELL_MIN) in (fit.note or ""), fit.note


def test_non_adjacent_counts_are_not_compared():
    """A 2-bed against a 5-bed is a different kind of house, not three bedrooms."""
    rows = ([_Sale(1_100_000, 2, 1) for _ in range(10)]
            + [_Sale(2_400_000, 5, 3) for _ in range(10)])
    fit = _room_effect_by_cell(rows, "beds")
    assert fit.dollars is None, f"compared non-adjacent counts: {fit.note}"
