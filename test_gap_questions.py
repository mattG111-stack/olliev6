"""When Ollie can't answer, it asks for the one thing that would let it.

    "if it cant answer it should ask for the gap it has too answer the question"

Every tool that could not answer used to return a full stop:

    "No listings match those filters."
    "No comparable sales found in Riverhead for a House like that."
    "No sold records with a listing date for Riverhead."

All three are true, and none of them leaves the person anywhere to go. They also
hide which of several very different problems occurred — a misspelled suburb, a
price cap $50k short, and a genuinely empty market read identically.

The worst case was value_property. matched_sold_price returns nothing at all
unless it has BOTH a bed and a bath count — it matches on room count before
anything else — but the tool advertised only `suburb` as required. So "what's a
house in Riverhead worth" came back as "no comparable sales found", which reads
as a fact about Riverhead when the truth was that we never asked how big a
house. The user is told the market is empty; the market is fine.

Each of these now returns a CANNOT ANSWER YET block naming the missing thing and
carrying a ready-to-say question, plus whatever real figure we CAN give in the
meantime so the reply doesn't read as "start again".
"""
from __future__ import annotations

import pytest

from app.assistant import agent
from app.assistant.providers import _out_of_time, _ran_out
from app.assistant.tools import (_gap, _near, _no_batch, search_listings,
                                 suburb_days_to_sell, value_property)
from app.models import BatchType, ImportBatch, PropertyForSale, PropertySold

GAP = "CANNOT ANSWER YET"


def _sold(db, n=12, **over):
    b = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                    filename="sold.xlsx", is_active=True, status="published")
    db.add(b)
    db.flush()
    spec = dict(suburb="Riverhead", district="Rodney", property_type="House",
                beds=5, baths=2, floor=265, land=800, price=1_580_000,
                dom=None)
    spec.update(over)
    for i in range(n):
        db.add(PropertySold(
            import_batch_id=b.id, address=f"{i} Coatesville Road",
            suburb=spec["suburb"], district=spec["district"],
            property_type=spec["property_type"], beds=spec["beds"],
            baths=spec["baths"], floor_area_m2=spec["floor"] + i,
            land_area_m2=spec["land"] + i * 5,
            sale_price=spec["price"] + i * 15_000, cv_numeric=1_500_000,
            days_on_market=spec["dom"],
            sold_date="2026-06-01", type_of_title="Freehold"))
    db.commit()
    return b


def _live(db, n=6, **over):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="live.xlsx", is_active=True, status="published")
    db.add(b)
    db.flush()
    spec = dict(suburb="Riverhead", district="Rodney", beds=5,
                asking=1_800_000, underpriced=False)
    spec.update(over)
    for i in range(n):
        db.add(PropertyForSale(
            import_batch_id=b.id, address=f"{i} Elliot Street",
            suburb=spec["suburb"], district=spec["district"],
            property_type="House", beds=spec["beds"], baths=2,
            floor_area_m2=260, land_area_m2=800,
            asking_price=spec["asking"], cv_numeric=1_500_000,
            is_underpriced=spec["underpriced"]))
    db.commit()
    return b


# ---- the case that started it ---------------------------------------------
def test_no_room_counts_asks_for_them_instead_of_blaming_the_suburb(db_session):
    """The engine needs beds AND baths and returns nothing without them.

    The old reply — "no comparable sales found in Riverhead" — is a statement
    about Riverhead. Riverhead is fine. We were never told how big a house.
    """
    _sold(db_session)
    out = value_property(suburb="Riverhead")
    assert GAP in out
    assert "bedrooms and bathrooms" in out
    assert "How many" in out
    # And it must not read as an empty market.
    assert "no comparable sales" not in out.lower()


def test_one_room_count_asks_only_for_the_other(db_session):
    _sold(db_session)
    out = value_property(suburb="Riverhead", beds=5)
    assert "bathrooms" in out
    assert "bedrooms" not in out.split("Ask:")[1]


def test_it_still_gives_a_real_figure_while_it_asks(db_session):
    """A question back that hands over nothing reads as "start again".

    The suburb median is real data and answers a slightly different question,
    so it goes in the reply clearly labelled as the suburb rather than as the
    house being asked about.
    """
    _sold(db_session, n=12)
    out = value_property(suburb="Riverhead")
    assert "Can say meanwhile" in out
    assert "$1," in out
    assert "12 sales" in out
    assert "not this house" in out


def test_the_meanwhile_figure_is_withheld_when_the_sample_is_thin(db_session):
    """Four sales is not a suburb median and must not be quoted as one."""
    _sold(db_session, n=4)
    out = value_property(suburb="Riverhead")
    assert GAP in out
    assert "Can say meanwhile" not in out


# ---- spelling, which is the usual real cause -------------------------------
def test_an_unknown_suburb_offers_the_real_spellings(db_session):
    _sold(db_session, suburb="Riverhead")
    out = value_property(suburb="Riverhed", beds=5, baths=2)
    assert GAP in out
    assert "Did you mean" in out
    assert "Riverhead" in out


def test_a_suburb_we_genuinely_do_not_have_asks_which_one(db_session):
    _sold(db_session, suburb="Riverhead")
    out = value_property(suburb="Dunedin Central", beds=4, baths=2)
    assert GAP in out
    assert "which" in out.lower()
    assert "?" in out


def test_days_to_sell_separates_wrong_spelling_from_no_listing_dates(db_session):
    """Two different problems that used to produce the same sentence.

    A suburb with sales but no listing dates can never answer this question —
    no spelling fixes it — while a misspelled one is answered by a name. The
    old message ("No sold records with a listing date for X") covered both.
    """
    _sold(db_session, suburb="Riverhead", dom=None)

    misspelled = suburb_days_to_sell("Riverhed")
    assert GAP in misspelled
    assert "Riverhead" in misspelled

    real = suburb_days_to_sell("Riverhead")
    assert GAP not in real
    assert "none of them carry a listing date" in real
    assert "want those instead?" in real.lower()


# ---- which filter is the wall ---------------------------------------------
def test_it_names_the_filter_that_emptied_the_search(db_session):
    """Six filters on and nothing back: the person cannot tell whether the
    suburb is empty or the price cap is short by $50k."""
    _live(db_session, suburb="Riverhead", asking=1_800_000)
    out = search_listings(suburb="Riverhead", max_price=900_000)
    assert GAP in out
    # "under $900,000", not "asking under $900,000". Four listings in five have
    # no asking price, so the budget is matched on the vendor's price where
    # there is one and our valuation where there is not — and naming it after
    # the asking price would describe a filter that is not the one being run.
    assert "under $900,000" in out
    assert "Which shall I relax" in out


def test_a_single_filter_that_matches_nothing_offers_a_wider_look(db_session):
    _live(db_session, suburb="Riverhead")
    out = search_listings(suburb="Coatesville")
    assert GAP in out
    assert "wider area" in out


def test_it_reports_how_many_each_dropped_filter_would_find(db_session):
    _live(db_session, n=6, suburb="Riverhead", asking=1_800_000)
    out = search_listings(suburb="Riverhead", max_price=900_000)
    assert "6" in out                       # dropping the cap finds all six


# ---- no data loaded is a gap with an owner ---------------------------------
def test_nothing_loaded_says_what_to_do_about_it(db_session):
    out = value_property(suburb="Riverhead", beds=5, baths=2)
    assert GAP in out
    assert "publish" in out.lower()


# ---- the shape itself ------------------------------------------------------
def test_a_gap_always_ends_in_a_question():
    out = _gap(need="a bed count", ask="How many bedrooms?")
    assert out.startswith(GAP)
    assert out.rstrip().endswith("?")


def test_a_gap_tells_the_model_not_to_guess():
    """Without this the model fills the hole itself and answers confidently
    about a house nobody described."""
    assert "DO NOT GUESS" in _gap(need="x", ask="y?")
    assert "DO NOT GUESS" in _no_batch("sold")


def test_near_matches_come_from_the_data_never_from_a_guess():
    real = ["Riverhead", "Kumeu", "Huapai"]
    assert _near("Riverhed", real) == ["Riverhead"]
    assert _near("Whangaparaoa", real) == []
    assert _near("", real) == []


def test_near_matches_are_case_and_substring_tolerant():
    real = ["Browns Bay", "Mairangi Bay"]
    assert "Browns Bay" in _near("browns bay", real)
    assert "Browns Bay" in _near("Browns", real)


# ---- giving up ends in a question too --------------------------------------
def test_running_out_of_time_asks_how_to_narrow_it():
    out = _out_of_time(["query_data", "distinct_values"])
    assert out.rstrip().endswith("?")
    assert "median, a count, or a ranking" in out


def test_running_out_of_steps_asks_too():
    assert _ran_out(["search_listings"]).rstrip().endswith("?")
    assert _ran_out([]).rstrip().endswith("?")


def test_the_advice_matches_what_actually_ran():
    """"Try something narrower" puts the diagnosis back on the person asking.
    What ran is known here and says which narrowing is the useful one."""
    assert "area" in _out_of_time(["search_listings"]).lower()
    assert "single number" in _out_of_time(["query_data"]).lower()


# ---- the instruction that makes it land ------------------------------------
def test_the_system_prompt_tells_the_model_to_relay_the_ask():
    assert GAP in agent.SYSTEM
    assert "NEVER DEAD-END" in agent.SYSTEM
    assert "ends in a question mark" in agent.SYSTEM


def test_the_system_prompt_forbids_working_around_the_gap():
    """The failure mode this replaces: the model saw "no results", widened the
    query itself, and answered a question nobody asked."""
    low = agent.SYSTEM.lower()
    assert "do not substitute your own assumption" in low
    assert "approximate around the gap" in low


# ---- an address is not an address without a suburb -------------------------
#
# "12 Elliot Street" is not one place. Auckland has seven Queen Streets and a
# dozen Elliot Streets, and answering about whichever row came back first means
# answering confidently about a house twenty kilometres and four hundred
# thousand dollars away, with nothing on screen to say which one it picked.

def _at(db, address, suburb, district="Rodney", price=1_580_000):
    b = db.query(ImportBatch).filter(
        ImportBatch.batch_type == BatchType.SOLD.value).first()
    if b is None:
        b = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                        filename="sold.xlsx", is_active=True, status="published")
        db.add(b)
        db.flush()
    db.add(PropertySold(import_batch_id=b.id, address=address, suburb=suburb,
                        district=district, property_type="House", beds=4, baths=2,
                        floor_area_m2=200, land_area_m2=700, sale_price=price,
                        cv_numeric=1_500_000, sold_date="2026-06-01"))
    db.commit()
    return b


def test_one_street_name_in_two_suburbs_asks_which(db_session):
    from app.assistant.tools import find_address
    _at(db_session, "12 Elliot Street", "Riverhead")
    _at(db_session, "12 Elliot Street", "Papakura", district="Papakura")
    out = find_address("12 Elliot Street")
    assert GAP in out
    assert "Riverhead" in out and "Papakura" in out
    assert "Which do you mean" in out
    assert out.rstrip().endswith("?")


def test_it_never_silently_picks_one_of_them(db_session):
    """The whole failure: a confident answer about the wrong house."""
    from app.assistant.tools import find_address
    _at(db_session, "12 Elliot Street", "Riverhead", price=1_580_000)
    _at(db_session, "12 Elliot Street", "Papakura", district="Papakura",
        price=740_000)
    out = find_address("12 Elliot Street")
    assert "$1,580,000" not in out and "$740,000" not in out


def test_naming_the_suburb_answers_it(db_session):
    from app.assistant.tools import find_address
    _at(db_session, "12 Elliot Street", "Riverhead", price=1_580_000)
    _at(db_session, "12 Elliot Street", "Papakura", district="Papakura",
        price=740_000)
    out = find_address("12 Elliot Street", suburb="Papakura")
    assert GAP not in out
    assert "Papakura" in out
    assert "740,000" in out


def test_one_suburb_only_does_not_ask_needlessly(db_session):
    """Asking when there is nothing to disambiguate is just friction."""
    from app.assistant.tools import find_address
    _at(db_session, "12 Elliot Street", "Riverhead")
    out = find_address("12 Elliot Street")
    assert GAP not in out
    assert "Riverhead" in out


def test_the_wrong_suburb_offers_the_ones_that_do_have_it(db_session):
    """"Not found in Papakura" is a worse answer than "I have it in Riverhead"."""
    from app.assistant.tools import find_address
    _at(db_session, "12 Elliot Street", "Riverhead")
    out = find_address("12 Elliot Street", suburb="Papakura")
    assert GAP in out
    assert "Riverhead" in out
    assert "which one?" in out.lower()


def test_an_address_we_hold_nowhere_offers_to_value_one_like_it(db_session):
    from app.assistant.tools import find_address
    _at(db_session, "12 Elliot Street", "Riverhead")
    out = find_address("999 Nowhere Terrace")
    assert GAP in out
    assert "bedrooms and bathrooms" in out


def test_the_address_tool_is_declared_with_an_optional_suburb():
    from app.assistant.tools import TOOL_SPECS
    spec = next(t for t in TOOL_SPECS if t["name"] == "find_address")
    assert spec["parameters"]["required"] == ["address"]
    assert "suburb" in spec["parameters"]["properties"]
    assert "guessing" in spec["parameters"]["properties"]["suburb"]["description"]
