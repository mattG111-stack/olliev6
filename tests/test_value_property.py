"""The question Ollie could not answer.

    what's a 5 bedroom 2 bathroom 270sqm house and 810sqm of land worth
    in riverhead
    → HTTP 500 with no response body

Fixing the timeout was necessary and was not the answer. Ollie had no tool for
this at all. Every valuation tool it owned needed a property_id — an existing
row — and that house does not exist and is not listed. So the question fell to
raw SQL: guess the schema, guess the column names, widen the filter when
nothing comes back, compute a median by hand. Six or seven model calls, each of
them slow, for something the site itself answers in one function call.

Worse than slow: a hand-rolled SQL median would be a DIFFERENT number from the
one the deal page shows for the same house, and both would look authoritative.

value_property runs matched_sold_price — the same comp engine the pricing
pipeline runs — so the two cannot disagree.
"""
from __future__ import annotations

import re

import pytest

from app.assistant.tools import TOOL_SPECS, _HANDLERS, value_property
from app.models import BatchType, ImportBatch, PropertySold


def _riverhead(db, n=12, **over):
    b = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                    filename="sold.xlsx", is_active=True, status="published")
    db.add(b)
    db.flush()
    spec = dict(suburb="Riverhead", district="Rodney", property_type="House",
                beds=5, baths=2, floor=265, land=800, price=1_580_000)
    spec.update(over)
    for i in range(n):
        db.add(PropertySold(
            import_batch_id=b.id, address=f"{i} Coatesville Road",
            suburb=spec["suburb"], district=spec["district"],
            property_type=spec["property_type"], beds=spec["beds"],
            baths=spec["baths"], floor_area_m2=spec["floor"] + i,
            land_area_m2=spec["land"] + i * 5,
            sale_price=spec["price"] + i * 15_000, cv_numeric=1_500_000,
            sold_date="2026-06-01", type_of_title="Freehold"))
    db.commit()
    return b


# ---- the question itself ---------------------------------------------------
def test_it_answers_the_question_that_started_this(db_session):
    _riverhead(db_session)
    out = value_property(suburb="Riverhead", beds=5, baths=2,
                         floor_area_m2=270, land_area_m2=810)
    assert "Riverhead" in out
    assert "$1," in out, out                    # an actual number
    assert "12 comparable" in out


def test_the_property_does_not_have_to_exist(db_session):
    """The whole gap. get_sold_comparables needs a property_id; a house someone
    is thinking about buying has no id and is not in the database."""
    _riverhead(db_session)
    out = value_property(suburb="Riverhead", beds=5, baths=2)
    assert "worth about" in out


def test_it_says_how_it_matched_in_english(db_session):
    """The engine returns "suburb_land_floor". That is a variable name, and it
    was going straight into a customer sentence."""
    _riverhead(db_session)
    out = value_property(suburb="Riverhead", beds=5, baths=2,
                         floor_area_m2=270, land_area_m2=810)
    assert "_" not in out.split("matched on")[1].split(".")[0]
    assert "within 20%" in out


def test_it_says_how_many_sales_it_found(db_session):
    """A median of three is not a valuation, and the reader has to be able to
    see that rather than take a confident-looking number on trust."""
    _riverhead(db_session)
    assert "12 comparable" in value_property(suburb="Riverhead", beds=5, baths=2)


def test_a_thin_match_warns(db_session):
    """Between three and eight sales it answers, and says not to lean on it."""
    _riverhead(db_session, n=4)
    out = value_property(suburb="Riverhead", beds=5, baths=2)
    assert "rough guide" in out or "not enough" in out


def test_no_comparable_sales_asks_rather_than_reporting_an_empty_market(db_session):
    """A suburb spelled differently in the data looks exactly like a suburb
    with no sales — and telling the reader "no comparable sales found" picks
    the wrong one of those two silently. It asks which suburb instead."""
    _riverhead(db_session)
    out = value_property(suburb="Nowheresville", beds=5, baths=2)
    assert "CANNOT ANSWER YET" in out
    assert "No comparable sales" not in out
    assert out.rstrip().endswith("?")


def test_no_sold_data_at_all_is_not_a_crash(db_session):
    """Nothing loaded is a gap with an owner: it says what to publish."""
    out = value_property(suburb="Riverhead", beds=5, baths=2)
    assert "CANNOT ANSWER YET" in out
    assert "publish" in out.lower()


# ---- it must agree with the site -------------------------------------------
def test_it_gives_the_same_answer_as_the_pricing_engine(db_session):
    """The reason this is a tool and not a prompt telling the model to write
    SQL. A hand-rolled median would be a different number from the one the deal
    page shows for the same house, and both would look authoritative."""
    from app.pricing.buyprice import CompEngine
    from app.reprice import _sold_df

    _riverhead(db_session)
    sold = _sold_df(db_session, "Auckland")
    price, _tier, n = CompEngine(sold).matched_sold_price(
        suburb="Riverhead", district=None, property_type="House",
        beds=5, baths=2, land=810, floor=270)

    out = value_property(suburb="Riverhead", beds=5, baths=2,
                         floor_area_m2=270, land_area_m2=810)
    shown = float(re.search(r"\$([\d,]+)", out).group(1).replace(",", ""))
    # Displayed to the nearest thousand, so compare the numbers rather than the
    # strings — what must not differ is the VALUATION, not its formatting.
    assert abs(shown - price) / price < 0.001, f"{shown} vs {price}"
    assert f"{n} comparable" in out


def test_it_never_quotes_a_council_valuation(db_session):
    """Sold prices only. The CV is on every one of these records and is not what
    was asked for — "what is it worth" means what people paid."""
    _riverhead(db_session)
    out = value_property(suburb="Riverhead", beds=5, baths=2)
    assert "1,500,000" not in out               # the CV on every comp
    assert "ACTUALLY SOLD FOR" in out


# ---- and Ollie has to reach for it -----------------------------------------
def test_the_tool_is_registered_and_offered_first():
    """It is listed before query_data on purpose. Given raw SQL and a valuation
    tool, a model will happily spend six turns writing SQL."""
    assert "value_property" in _HANDLERS
    names = [t["name"] for t in TOOL_SPECS]
    assert names[0] == "value_property"
    assert names.index("value_property") < names.index("query_data")


def test_its_description_tells_the_model_not_to_hand_roll_it():
    spec = next(t for t in TOOL_SPECS if t["name"] == "value_property")
    text = spec["description"].lower()
    assert "what is x worth" in text or "worth" in text
    assert "sql" in text                         # the explicit "do not" 


def test_only_the_suburb_is_required():
    """Half the questions people ask do not carry every measurement."""
    spec = next(t for t in TOOL_SPECS if t["name"] == "value_property")
    assert spec["parameters"]["required"] == ["suburb"]
