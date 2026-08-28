"""A price change has to be a decision somebody made.

    "$840k → $5.05M, +501.2%" — Ranui
    "$1.01M → $4.55M, +350.5%" — Henderson
    "these numbers dont look right either"

They are not price rises. They are one week where the scraper wrote the council
valuation into the price field and one week where it wrote a real asking price,
subtracted from each other and printed as market news. Every headline rise on
that batch was this shape.

app.prior_price.is_placeholder_price is the rule for "did a vendor actually name
this number", and it has three callers. This panel was a fourth place that asked
the question and never used it — the only filter was `asking_price >= 10000`,
which catches a $1 placeholder and nothing else.

Two things fix it, and the second matters as much as the first:

  BOTH SIDES MUST BE A REAL PRICE. A comparison is only a price change if both
  ends were prices.

  AND IT MUST BE BELIEVABLE. A vendor who re-prices moves by ten or fifteen
  percent. Beyond half, something about how the figure was captured changed, and
  whatever that is, it is not this week's market news.
"""
from __future__ import annotations

import pytest

from app.models import ImportBatch, PropertyForSale
from app.routers.dashboards import MAX_BELIEVABLE_PRICE_MOVE


def _batch(db, filename: str) -> ImportBatch:
    b = ImportBatch(batch_type="for_sale", region="Auckland", filename=filename,
                    is_active=True, status="published")
    db.add(b); db.flush()
    return b


def _row(db, batch, slug, asking, *, cv=1_200_000.0, last_sold=None):
    db.add(PropertyForSale(
        import_batch_id=batch.id, region="Auckland", slug_id=slug,
        address=f"{slug} Road", suburb="Ranui", property_type="House",
        asking_price=asking, cv_numeric=cv,
        valuation_last_sold_value=last_sold, floor_area_m2=140.0))


def _movers(db, prev, curr):
    """The panel's own query, run the way the endpoint runs it."""
    from sqlalchemy import text

    from app.routers import dashboards

    sql = dashboards.__dict__  # keep the import honest
    assert sql is not None
    from app.routers.dashboards import today_brief  # noqa: F401
    # Re-run the same statement the brief uses, via the module's constant.
    stmt = text(_MOVERS_SQL)
    return db.execute(stmt, {"prev": prev.id, "curr": curr.id,
                             "max_move": MAX_BELIEVABLE_PRICE_MOVE}).fetchall()


# The statement under test, lifted from dashboards.today_brief so the test
# exercises the real thing rather than a paraphrase of it.
def _extract_sql() -> str:
    import inspect
    import re

    from app.routers import dashboards

    src = inspect.getsource(dashboards)
    m = re.search(r'"""\s*(\n\s*--.*?SELECT id, slug_id, address, suburb, pa, pb, change_pct, kind FROM ri\s*\n\s*)"""',
                  src, re.S)
    assert m, "could not find the movers statement — has it been rewritten?"
    return m.group(1)


_MOVERS_SQL = _extract_sql()


def test_a_placeholder_on_either_side_is_not_a_price_change(db_session):
    """The Ranui headline. Last week the price field held the council
    valuation; this week it holds a real asking. Nothing about the property
    changed and no vendor did anything."""
    prev, curr = _batch(db_session, "week1"), _batch(db_session, "week2")
    _row(db_session, prev, "ranui-1", 840_000.0, cv=840_000.0)   # CV in the field
    _row(db_session, curr, "ranui-1", 5_050_000.0, cv=840_000.0)
    db_session.commit()

    assert _movers(db_session, prev, curr) == []


def test_a_last_sale_price_in_the_field_is_not_a_price_either(db_session):
    prev, curr = _batch(db_session, "week1"), _batch(db_session, "week2")
    _row(db_session, prev, "x-1", 340_000.0, cv=1_200_000.0, last_sold=340_000.0)
    _row(db_session, curr, "x-1", 1_170_000.0, cv=1_200_000.0, last_sold=340_000.0)
    db_session.commit()

    assert _movers(db_session, prev, curr) == []


def test_a_move_too_big_to_be_a_decision_is_left_out(db_session):
    """Both ends real prices, and still not a re-pricing."""
    prev, curr = _batch(db_session, "week1"), _batch(db_session, "week2")
    _row(db_session, prev, "y-1", 800_000.0)
    _row(db_session, curr, "y-1", 4_000_000.0)          # +400%
    db_session.commit()

    assert _movers(db_session, prev, curr) == []


def test_a_real_re_pricing_still_shows(db_session):
    """The counterweight. A vendor dropping their price is the signal the panel
    exists for, and a filter that removes those has replaced one wrong answer
    with another."""
    prev, curr = _batch(db_session, "week1"), _batch(db_session, "week2")
    _row(db_session, prev, "z-1", 1_100_000.0)
    _row(db_session, curr, "z-1", 949_000.0)           # −13.7%, an ordinary drop
    db_session.commit()

    rows = _movers(db_session, prev, curr)
    assert len(rows) == 1
    assert rows[0][7] == "drop"
    assert rows[0][1] == "z-1"
    assert rows[0][6] == pytest.approx(-0.1373, abs=0.001)


@pytest.mark.parametrize("pct", [-0.45, -0.30, 0.20, 0.45])
def test_the_band_keeps_everything_inside_it(db_session, pct):
    prev, curr = _batch(db_session, "week1"), _batch(db_session, "week2")
    # A CV nowhere near either price: +20% of $1,000,000 is $1,200,000, and with
    # the fixture's default CV that IS $1,200,000 — correctly filtered as a
    # placeholder, which made this look like the band was wrong.
    _row(db_session, prev, "b-1", 1_000_000.0, cv=3_000_000.0)
    _row(db_session, curr, "b-1", 1_000_000.0 * (1 + pct), cv=3_000_000.0)
    db_session.commit()

    rows = _movers(db_session, prev, curr)
    assert len(rows) == 1
    # And in the right column. The two lists were the five smallest and the five
    # largest of one set, so in a thin week a price CUT was also printed as the
    # week's biggest rise.
    assert rows[0][7] == ("drop" if pct < 0 else "rise")


def test_the_band_is_not_so_tight_it_hides_a_real_capitulation(db_session):
    """Half is deliberately generous: a vendor who gives up and drops 45% is
    exactly what somebody reading this panel wants to know about."""
    assert MAX_BELIEVABLE_PRICE_MOVE >= 0.45
