"""What it was listed at last week.

    "if a house is in the last file for sale with a price and now it is price
     by negtion we add that price it was before -50k"
    "and we have was list at $ whatever it was on the listing at this date"

A house advertised at $1,250,000 that comes back in the next load "by
negotiation" is not a house with no price — the vendor has stopped naming one,
which in this market means the number is coming down. But the only figure the
feed carries for those listings is a search price, set low so the listing shows
up in buyers' filters, so the pipeline throws it away and the listing goes dark.
455 listings on the last batch, a large share of which we had a real advertised
price for seven days earlier.

So the price is carried forward, less $50,000, and the listing says what it was
listed at and when.

The risk in doing this at all is that a number we derived becomes
indistinguishable from a number a vendor named. Half these tests are about that:
the basis travels with the price everywhere, a real price is never overwritten,
and a prior "price" that was itself a placeholder is never laundered into a fact
by being moved between two loads.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.models import BatchType, ImportBatch, PropertyForSale
from app.pricing.pipeline import run as run_pipeline
from app.prior_price import (ADVERTISED_BASIS, DERIVED_BASIS, ROUND_TO,
                             address_key, carry_forward_prices, derived_asking)

_LAST_WEEK = datetime(2026, 8, 3, tzinfo=timezone.utc)
_TODAY = datetime(2026, 8, 10, tzinfo=timezone.utc)


def _batch(db, when, **kw):
    d = dict(batch_type=BatchType.FOR_SALE.value, region="Auckland",
             filename=f"auckland-{when:%d-%m}.csv", is_active=False,
             status="staged", created_at=when)
    d.update(kw)
    b = ImportBatch(**d)
    db.add(b)
    db.commit()
    return b


def _row(db, batch, address="12 Elliot Street", **kw):
    d = dict(import_batch_id=batch.id, address=address, suburb="Remuera",
             property_type="House", cv_numeric=1_200_000.0, floor_area_m2=150.0)
    d.update(kw)
    p = PropertyForSale(**d)
    db.add(p)
    db.commit()
    return p


# ---- the thing that was asked for ------------------------------------------
def test_last_weeks_price_less_50k_is_carried_onto_this_weeks_listing(db_session):
    old = _batch(db_session, _LAST_WEEK)
    _row(db_session, old, asking_price=1_250_000.0, asking_basis=ADVERTISED_BASIS)

    new = _batch(db_session, _TODAY)
    now = _row(db_session, new, asking_price=None)

    assert carry_forward_prices(db_session, new.id) == 1
    db_session.refresh(now)
    assert now.prior_asking_price == 1_250_000.0
    # SQLite drops the timezone on the way back out, so compare the instant.
    seen = now.prior_asking_seen_at
    assert seen.replace(tzinfo=timezone.utc) == _LAST_WEEK, \
        "the date has to be the load it was advertised in, not today"


def test_the_pipeline_prices_it_from_that_figure_less_50k(db_session):
    """The point of the whole change: the listing gets a working price again."""
    df = pd.DataFrame([{
        "address": "12 Elliot Street", "suburb": "Remuera", "property_type": "House",
        "price_display": "By negotiation", "price_numeric": 700_000.0,   # search price
        "prior_asking_price": 1_250_000.0,
        "cv_numeric": 1_200_000.0, "key_floor_area": 150.0, "key_land_area": 500.0,
        "key_bedrooms": 3, "key_bathrooms": 2,
    }])
    out = run_pipeline(df, _sold(), None)

    assert out.loc[0, "price_numeric"] == 1_210_000.0   # 3% off, to the nearest $10k
    assert out.loc[0, "asking_basis"] == DERIVED_BASIS
    assert out.loc[0, "price_numeric"] != 700_000.0, \
        "the search price is never the price — that is what started this"


def test_without_a_prior_price_it_still_has_none(db_session):
    """A listing that has ALWAYS been by negotiation is unchanged — there is no
    honest number to give it."""
    df = pd.DataFrame([{
        "address": "9 Never Priced Road", "suburb": "Remuera", "property_type": "House",
        "price_display": "By negotiation", "price_numeric": 700_000.0,
        "cv_numeric": 1_200_000.0, "key_floor_area": 150.0, "key_land_area": 500.0,
        "key_bedrooms": 3, "key_bathrooms": 2,
    }])
    out = run_pipeline(df, _sold(), None)

    assert pd.isna(out.loc[0, "price_numeric"])
    assert not out.loc[0, "asking_basis"]


# ---- the risk: a derived number passing as an advertised one ---------------
def test_the_basis_travels_with_the_price(db_session):
    """Everywhere the number goes, the sentence goes. A derived figure that
    cannot be told apart from an advertised one is the whole risk here."""
    df = pd.DataFrame([{
        "address": "12 Elliot Street", "suburb": "Remuera", "property_type": "House",
        "price_display": "By negotiation", "price_numeric": 700_000.0,
        "prior_asking_price": 1_250_000.0,
        "cv_numeric": 1_200_000.0, "key_floor_area": 150.0, "key_land_area": 500.0,
        "key_bedrooms": 3, "key_bathrooms": 2,
    }])
    out = run_pipeline(df, _sold(), None)

    assert out.loc[0, "asking_basis"] == DERIVED_BASIS
    assert "3%" in out.loc[0, "asking_basis"], \
        "the sentence has to say what was taken off, not just that something was"


def test_an_advertised_price_is_never_overwritten(db_session):
    """If the vendor is naming a price today, that is the price."""
    old = _batch(db_session, _LAST_WEEK)
    _row(db_session, old, asking_price=1_250_000.0, asking_basis=ADVERTISED_BASIS)

    new = _batch(db_session, _TODAY)
    now = _row(db_session, new, asking_price=1_100_000.0, asking_basis=ADVERTISED_BASIS)

    carry_forward_prices(db_session, new.id)
    db_session.refresh(now)
    assert now.asking_price == 1_100_000.0
    assert now.prior_asking_price is None


def test_a_placeholder_price_is_never_carried_forward(db_session):
    """The prior row's "price" was the scraper filling in the council valuation.
    Moving it between two loads would launder a guess into a fact."""
    old = _batch(db_session, _LAST_WEEK)
    _row(db_session, old, asking_price=1_200_000.0, cv_numeric=1_200_000.0)  # == CV

    new = _batch(db_session, _TODAY)
    now = _row(db_session, new, asking_price=None)

    assert carry_forward_prices(db_session, new.id) == 0
    db_session.refresh(now)
    assert now.prior_asking_price is None


def test_a_derived_price_is_never_re_derived(db_session):
    """Otherwise a listing by negotiation for four weeks loses $200,000 to
    compounding, and every week's figure looks as solid as the first."""
    old = _batch(db_session, _LAST_WEEK)
    _row(db_session, old, asking_price=1_200_000.0, asking_basis=DERIVED_BASIS)

    new = _batch(db_session, _TODAY)
    now = _row(db_session, new, asking_price=None)

    assert carry_forward_prices(db_session, new.id) == 0
    db_session.refresh(now)
    assert now.prior_asking_price is None


def test_the_derived_price_is_a_price_somebody_would_say(db_session):
    """3% of $1,250,000 is $1,212,500 and no agent in the country says that
    number. Printing it to the dollar also claims a precision we do not have —
    it is a derived figure, not a quote."""
    assert derived_asking(1_250_000) == 1_210_000
    assert derived_asking(600_000) == 580_000
    assert derived_asking(3_000_000) == 2_910_000
    for v in (1_250_000, 600_000, 3_000_000, 330_000, 899_000):
        assert derived_asking(v) % ROUND_TO == 0


def test_a_price_too_small_to_survive_the_discount_is_not_carried(db_session):
    """A zero or negative asking would sort to the top of every deal list."""
    assert derived_asking(0) is None
    assert derived_asking(None) is None
    assert derived_asking(-5_000) is None
    df = pd.DataFrame([{
        "address": "1 Tiny Lane", "suburb": "Remuera", "property_type": "House",
        "price_display": "By negotiation", "price_numeric": 30_000.0,
        "prior_asking_price": 1_000.0,
        "cv_numeric": 1_200_000.0, "key_floor_area": 150.0, "key_land_area": 500.0,
        "key_bedrooms": 3, "key_bathrooms": 2,
    }])
    out = run_pipeline(df, _sold(), None)
    assert pd.isna(out.loc[0, "price_numeric"])


# ---- matching the same house across two weeks of scraped text --------------
def test_the_same_house_is_matched_through_a_changed_abbreviation(db_session):
    """The feeds are not consistent between weeks. Matching on the raw string
    finds almost nothing and reports "no prior price" for houses we have one for."""
    assert address_key("12 Elliot St, Remuera") == address_key("12 Elliot Street Remuera")
    assert address_key("8 Kotare Ave.", "Devonport") == address_key("8 Kotare Avenue", "Devonport")


def test_two_flats_in_one_building_are_not_the_same_house(db_session):
    """building_key() strips unit numbers because it is answering "which
    building". This is the opposite question, and getting it wrong would put
    2/14's price on 5/14."""
    assert address_key("2/14 Queen Street") != address_key("5/14 Queen Street")


def test_a_different_suburb_is_a_different_house(db_session):
    assert address_key("1 High Street", "Remuera") != address_key("1 High Street", "Otahuhu")


def test_the_most_recent_advertised_price_wins(db_session):
    """Three loads: priced, then priced lower, then withdrawn. The one that
    counts is the last real one, not the first."""
    a = _batch(db_session, _LAST_WEEK - timedelta(days=7))
    _row(db_session, a, asking_price=1_400_000.0, asking_basis=ADVERTISED_BASIS)
    b = _batch(db_session, _LAST_WEEK)
    _row(db_session, b, asking_price=1_250_000.0, asking_basis=ADVERTISED_BASIS)

    new = _batch(db_session, _TODAY)
    now = _row(db_session, new, asking_price=None)

    carry_forward_prices(db_session, new.id)
    db_session.refresh(now)
    assert now.prior_asking_price == 1_250_000.0


def test_running_it_twice_changes_nothing(db_session):
    """The price stage is re-runnable, and a carry-forward that drifts on each
    run would walk the price down every time somebody pressed the button."""
    old = _batch(db_session, _LAST_WEEK)
    _row(db_session, old, asking_price=1_250_000.0, asking_basis=ADVERTISED_BASIS)
    new = _batch(db_session, _TODAY)
    now = _row(db_session, new, asking_price=None)

    carry_forward_prices(db_session, new.id)
    carry_forward_prices(db_session, new.id)
    db_session.refresh(now)
    assert now.prior_asking_price == 1_250_000.0


def test_a_later_load_is_never_read_as_a_prior_one(db_session):
    """Re-pricing an old batch must not reach forward into a newer one."""
    new = _batch(db_session, _TODAY)
    _row(db_session, new, asking_price=1_250_000.0, asking_basis=ADVERTISED_BASIS)

    old = _batch(db_session, _LAST_WEEK)
    then = _row(db_session, old, asking_price=None)

    carry_forward_prices(db_session, old.id)
    db_session.refresh(then)
    assert then.prior_asking_price is None


# ---- a tiny sold set, enough for the pipeline to run ----------------------
def _sold():
    from app.pricing.comps import SoldDataset

    return SoldDataset(pd.DataFrame([{
        "address": f"{i} Comparable Road", "suburb": "Remuera", "district": "Auckland",
        "property_type": "House", "price_numeric": 1_200_000.0 + i * 1_000,
        "sale_price": 1_200_000.0 + i * 1_000,
        "cv_numeric": 1_180_000.0, "key_floor_area": 150.0, "key_land_area": 500.0,
        "key_bedrooms": 3, "key_bathrooms": 2, "sold_date": "2026-06-01",
        "days_on_market": 30,
    } for i in range(40)]))


# ---- the bug the rounding introduced ---------------------------------------
#
# Found by working an actual case rather than by a test failing: the $10,000
# rounding lands the carried figure on a round council valuation often enough to
# matter. A vendor asking $1,340,000 against a $1,300,000 CV derives to exactly
# $1,300,000, and three separate guards — the pipeline, the pre-publish hold and
# the browse-list filter — all guess "asking == CV means the scraper invented
# it" and threw the listing out. That is the precise listing the carry-forward
# exists to rescue, so the rescue undid itself on a rounding boundary.
#
# The fix is not to nudge the number. asking_basis RECORDS where the price came
# from, so a guess about where it came from does not apply on top of it.
def test_a_carried_price_landing_on_the_cv_is_not_mistaken_for_a_fake_one(db_session):
    from app.models import PropertyForSale
    from app.release import _asking_is_placeholder, _hold_reason

    ask = derived_asking(1_340_000.0)
    assert ask == 1_300_000.0, "the collision this guards against has moved"

    p = PropertyForSale(address="12 Elliot Street", asking_price=ask,
                        cv_numeric=1_300_000.0, fair_value=1_500_000.0,
                        floor_area_m2=150.0, property_type="House",
                        asking_basis=DERIVED_BASIS)
    assert not _asking_is_placeholder(p)
    assert _hold_reason(p) is None, \
        "the listing the carry-forward exists to rescue was held anyway"


def test_a_scraper_filling_in_the_cv_is_still_caught(db_session):
    """The counterweight. The check is switched off for prices whose provenance
    we know, not for prices that happen to equal the CV — a feed price sitting
    exactly on the council valuation is still the scraper filling a blank."""
    from app.models import PropertyForSale
    from app.release import _asking_is_placeholder

    p = PropertyForSale(address="12 Elliot Street", asking_price=1_300_000.0,
                        cv_numeric=1_300_000.0, floor_area_m2=150.0,
                        property_type="House", asking_basis=ADVERTISED_BASIS)
    assert _asking_is_placeholder(p)


def test_the_browse_list_does_not_hide_a_carried_price_either(db_session):
    """Three guards made the same guess and all three had to be fixed. This one
    is raw SQL, so it cannot be covered by testing the Python path."""
    from app.models import BatchType, ImportBatch, PropertyForSale
    from app.routers.properties import _hide_bad_data

    b = _batch(db_session, _TODAY)
    ask = derived_asking(1_340_000.0)
    db_session.add(PropertyForSale(
        import_batch_id=b.id, address="12 Elliot Street", suburb="Remuera",
        property_type="House", asking_price=ask, cv_numeric=1_300_000.0,
        fair_value=1_500_000.0, margin=0.15, floor_area_m2=150.0,
        is_held=False, asking_basis=DERIVED_BASIS))
    db_session.commit()

    visible = _hide_bad_data(
        db_session.query(PropertyForSale).filter(
            PropertyForSale.import_batch_id == b.id)).count()
    assert visible == 1, "the browse list hid a listing we had a real price for"
