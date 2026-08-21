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


# --- the Mount Eden figures: +$950k a bedroom on a $1.79M median -------------

class _Typed(_Sale):
    """A sale that knows what KIND of home it is."""
    def __init__(self, price, beds, baths, ptype, floor=120.0, land=400.0):
        super().__init__(price, beds, baths, floor=floor, land=land)
        self.property_type = ptype


def _house_and_apartment_suburb(seed=3):
    """Houses at 3 and 4 beds; apartments at 2 and 3, far cheaper.

    The shape that produced +$950k: a 3-bed cell holding cheap apartments and a
    4-bed cell holding only villas, so the "bedroom" was really the difference
    between an apartment and a house.
    """
    rnd = random.Random(seed)
    rows = []
    for _ in range(12):
        rows.append(_Typed(1_700_000 + rnd.gauss(0, 60_000), 3, 1, "House"))
    for _ in range(12):
        rows.append(_Typed(2_000_000 + rnd.gauss(0, 60_000), 4, 2, "House"))
    for _ in range(14):
        rows.append(_Typed(700_000 + rnd.gauss(0, 40_000), 3, 1, "Apartment"))
    for _ in range(10):
        rows.append(_Typed(600_000 + rnd.gauss(0, 40_000), 2, 1, "Apartment"))
    return rows


def test_an_apartment_is_not_a_smaller_house():
    """The comparison holds one kind of home, so beds are compared like for like."""
    fit = _room_effect_by_cell(_house_and_apartment_suburb(), "beds")
    assert fit.dollars is not None, fit.note
    # Houses: 3-bed ~$1.70M, 4-bed ~$2.00M. The apartments must not drag the
    # 3-bed cell down and turn a $300k step into a million-dollar one.
    assert 150_000 < fit.dollars < 600_000, f"got {fit.dollars} ({fit.note})"
    assert "house" in (fit.note or "").lower(), fit.note


def test_the_figure_says_which_kind_of_home_it_compared():
    fit = _room_effect_by_cell(_house_and_apartment_suburb(), "beds")
    assert "only" in (fit.note or ""), fit.note


def test_a_room_worth_half_the_suburb_is_withheld_not_published():
    """The guard that existed and was never called.

    ROOM_MAX_SHARE has been in the file the whole time — "an effect larger than
    this share of the local median is not a room" — inside a helper that nothing
    calls any more. So Mount Eden printed +$950k a bedroom and +$730k a bathroom
    against a $1.79M median, where the ceiling is $626k.
    """
    from app.routers.properties import ROOM_MAX_SHARE
    median = 1_790_000
    assert ROOM_MAX_SHARE * median < 950_000, (
        "the Mount Eden figure should sit above the bar"
    )
    # A suburb where the gap between adjacent counts really is enormous.
    rows = ([_Typed(900_000, 3, 1, "House") for _ in range(10)]
            + [_Typed(2_400_000, 4, 2, "House") for _ in range(10)])
    fit = _room_effect_by_cell(rows, "beds")
    assert fit.dollars == 1_500_000, fit.note      # the raw gap is real...
    # ...and the endpoint must refuse to publish it against this median.
    assert abs(fit.dollars) > ROOM_MAX_SHARE * 1_790_000


def test_the_endpoint_withholds_it_rather_than_printing_it(db_session):
    """Through suburb_stats, which is what the panel actually reads."""
    from app.models import BatchType, ImportBatch, PropertySold
    from app.routers.properties import suburb_stats

    db = db_session
    batch = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                        filename="rooms.csv", rows_total=0, is_active=True,
                        status="published")
    db.add(batch); db.flush()
    n = 0
    # A suburb where 3-beds and 4-beds are a million apart: the gap is real, but
    # it is not what one bedroom is worth.
    for beds, price, count in ((3, 900_000, 10), (4, 2_400_000, 10)):
        for _ in range(count):
            n += 1
            db.add(PropertySold(slug_id=f"r{n}", address=f"{n} Room Rd",
                                suburb="Mount Eden", region="Auckland",
                                sale_price=price, sold_date="2026-06-15",
                                beds=beds, baths=2, floor_area_m2=120,
                                land_area_m2=400, property_type="House",
                                import_batch_id=batch.id))
    batch.rows_total = n
    db.commit()

    stats = suburb_stats(suburb="Mount Eden", region="Auckland", from_year=None,
                         to_year=None, ptype=None, db=db)
    bedroom = next(e for e in stats.effects if e.key == "bedroom")
    assert bedroom.dollars is None, (
        f"published {bedroom.dollars} a bedroom against a median of {stats.median_sold}"
    )
    assert "too varied" in (bedroom.note or ""), bedroom.note
