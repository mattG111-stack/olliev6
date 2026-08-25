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


# ---- the reason it rescued nothing -----------------------------------------
#
# Measured on two real loads: 21 listings arrive with the price field empty, and
# 491 arrive with the council valuation copied into it verbatim. The second group
# IS the by-negotiation set — they are the rows held as "By negotiation - no real
# asking" — and asking "is the field empty" skipped every one of them. The
# feature ran, reported success, and carried a price onto nothing.
def test_a_listing_priced_at_its_council_valuation_needs_a_carried_price(db_session):
    from app.prior_price import needs_a_carried_price

    old = _batch(db_session, _LAST_WEEK)
    _row(db_session, old, asking_price=1_250_000.0, cv_numeric=1_200_000.0,
         asking_basis=ADVERTISED_BASIS)

    new = _batch(db_session, _TODAY)
    # The scraper copied the CV into the price field. It has a number; it does
    # not have a price.
    now = _row(db_session, new, asking_price=1_200_000.0, cv_numeric=1_200_000.0)

    assert needs_a_carried_price(now), "the 491 rows this exists for were skipped"
    assert carry_forward_prices(db_session, new.id) == 1
    db_session.refresh(now)
    assert now.prior_asking_price == 1_250_000.0


def test_the_pipeline_prices_over_a_council_valuation_placeholder(db_session):
    """End to end: the carried figure has to beat the placeholder, not sit
    behind it. Blanking the placeholder without replacing it is what the
    pipeline already did, and it left the listing dark."""
    df = pd.DataFrame([{
        "address": "12 Elliot Street", "suburb": "Remuera", "property_type": "House",
        "price_display": "$1,200,000", "price_numeric": 1_200_000.0,   # == the CV
        "prior_asking_price": 1_250_000.0,
        "cv_numeric": 1_200_000.0, "key_floor_area": 150.0, "key_land_area": 500.0,
        "key_bedrooms": 3, "key_bathrooms": 2,
    }])
    out = run_pipeline(df, _sold(), None)

    assert out.loc[0, "price_numeric"] == 1_210_000.0
    assert out.loc[0, "asking_basis"] == DERIVED_BASIS


def test_a_placeholder_with_no_history_is_still_not_a_price(db_session):
    """The counterweight. Nothing to carry means the placeholder stays refused —
    it does not get promoted to a real price just because we looked."""
    df = pd.DataFrame([{
        "address": "9 No History Road", "suburb": "Remuera", "property_type": "House",
        "price_display": "$1,200,000", "price_numeric": 1_200_000.0,
        "cv_numeric": 1_200_000.0, "key_floor_area": 150.0, "key_land_area": 500.0,
        "key_bedrooms": 3, "key_bathrooms": 2,
    }])
    out = run_pipeline(df, _sold(), None)
    assert out.loc[0, "asking_basis"] != DERIVED_BASIS
    assert pd.isna(out.loc[0, "margin"]), "a fake price still cannot make a deal"


def test_one_placeholder_rule_not_three(db_session):
    """The pipeline treated 491 listings as priced while release held the same
    491 as "no real asking" — two spellings of one idea, disagreeing. They now
    call the same function, so they cannot drift apart again."""
    from app.models import PropertyForSale
    from app.prior_price import is_placeholder_price
    from app.release import _asking_is_placeholder
    from app.routers.properties import _hide_bad_data          # noqa: F401

    cases = [(1_200_000.0, 1_200_000.0, None, True),      # == CV
             (1_250_000.0, 1_200_000.0, None, False),     # a real price
             (880_000.0, None, 880_000.0, True),          # == last sold
             (None, 1_200_000.0, None, False)]            # no price at all
    for ask, cv, ls, expected in cases:
        assert is_placeholder_price(ask, cv, ls) is expected, (ask, cv, ls)
        p = PropertyForSale(address="x", asking_price=ask, cv_numeric=cv,
                            valuation_last_sold_value=ls)
        assert _asking_is_placeholder(p) is expected, (ask, cv, ls)


# ---- a reason you can check --------------------------------------------------
#
# "so why didnt that show" — because the reason was worked out and thrown away,
# and when it finally was recorded it was too vague to act on. 150 The Drive,
# Epsom was refused at 76.3% against an 80% line: four points, on a $644,000
# margin backed by twelve sold comps. "Asking is far below the council
# valuation" does not let anybody see that. The numbers do.
def test_a_refusal_names_the_figures_that_caused_it(db_session):
    """Refused for thin evidence, not for the ratio — but the sentence still has
    to carry the numbers. "Asking is far below the council valuation" cannot be
    checked, argued with or acted on; it reads as a verdict when it is an
    arithmetic comparison."""
    df = pd.DataFrame([{
        "address": "150 The Drive", "suburb": "Remuera", "district": "Auckland",
        "property_type": "House", "price_display": "$2,500,000",
        "price_numeric": 2_500_000.0, "cv_numeric": 3_275_000.0,
        "improvement_value_numeric": 1_900_000.0, "land_value_numeric": 1_375_000.0,
        "key_floor_area": 122.0, "key_land_area": 1053.0,
        "key_bedrooms": 3, "key_bathrooms": 2, "type_of_title": "Freehold",
    }])
    out = run_pipeline(df, SoldDataset_thin(), None)
    why = str(out.loc[0, "deal_block_reason"] or "")
    if why and "comps" in why:
        assert "2,500,000" in why and "3,275,000" in why, why
        assert "76%" in why, "how far under the line it fell has to be visible"


def test_the_reason_fits_the_column_it_is_stored_in(db_session):
    """deal_block_reason is String(200). Postgres refuses a longer value outright,
    so a reason that grows past it fails the whole pricing run rather than being
    quietly clipped — and these are f-strings with prices interpolated into them."""
    df = pd.DataFrame([{
        "address": "1 Very Expensive Way", "suburb": "Remuera", "district": "Auckland",
        "property_type": "House", "price_display": "$12,500,000",
        "price_numeric": 12_500_000.0, "cv_numeric": 98_750_000.0,
        "key_floor_area": 122.0, "key_land_area": 1053.0,
        "key_bedrooms": 3, "key_bathrooms": 2, "type_of_title": "Freehold",
    }])
    out = run_pipeline(df, _sold(), None)
    for why in out["deal_block_reason"].dropna():
        assert len(str(why)) <= 200, why


# ---- what the asking-vs-CV floor was really for -----------------------------
#
#   "these houses have to show we are only trying to stop a house that have a
#    cv for land and is a new build"
#
# That sentence is the whole fix. DEAL_ASKING_CV_FLOOR was a PROXY for "the
# council valuation is wrong", and the system already detects that directly with
# is_land_only_cv(). So the floor was only ever deciding the case it was worst
# at: a house that is genuinely cheap against a council record we trust.
#
# 150 The Drive, Epsom was refused at 76.3% for missing an 80% line by four
# points, on a $644,000 margin that twelve size-controlled sold comps agreed
# with. A ratio is a proxy for evidence; comps ARE evidence. The proxy does not
# get to overrule them.
def _epsom(**kw):
    d = dict(suburb="Remuera", district="Auckland", property_type="House",
             key_floor_area=122.0, key_land_area=1053.0, key_bedrooms=3,
             key_bathrooms=2, type_of_title="Freehold")
    d.update(kw)
    return d


def test_a_cheap_house_with_real_sold_evidence_shows(db_session):
    """150 The Drive. Twelve recent sales say it is worth $3.1M and the asking is
    $2.5M. The council record is complete — improvement value present — so the
    ratio is measuring a cheap house, not a broken CV."""
    df = pd.DataFrame([_epsom(
        address="150 The Drive", price_display="$2,500,000",
        price_numeric=2_500_000.0, cv_numeric=3_275_000.0,
        improvement_value_numeric=1_900_000.0, land_value_numeric=1_375_000.0)])
    out = run_pipeline(df, SoldDataset_matching(), None)

    assert out.loc[0, "is_underpriced"], "the ratio overruled twelve sold comps"
    assert out.loc[0, "margin"] > 0
    assert not out.loc[0, "deal_block_reason"]


def test_a_new_build_on_a_land_only_council_record_does_not(db_session):
    """The case the floor exists for, and the only one it should catch. The
    council valued the dirt before the house went up, so the gap between the
    asking and the CV is a house against a section — not a discount, and no
    amount of sold evidence turns it into one."""
    df = pd.DataFrame([_epsom(
        address="1 New Build Way", price_display="$600,000",
        price_numeric=600_000.0, cv_numeric=900_000.0,
        improvement_value_numeric=None, land_value_numeric=900_000.0)])
    out = run_pipeline(df, SoldDataset_matching(), None)

    assert not out.loc[0, "is_underpriced"]
    why = str(out.loc[0, "deal_block_reason"])
    assert "land only" in why, why
    assert "house against dirt" in why, "the reason has to say what it caught"


def test_thin_evidence_under_the_floor_is_still_refused(db_session):
    """The counterweight, and the reason a flat 0.65 was the wrong answer. On a
    real load, dropping the floor to 0.65 returns 14 listings with fewer than
    three sold comps — a 42% "discount" off no evidence. The deal page sorts by
    margin, so those land ABOVE the genuine finds. Evidence is the override, not
    a lower number."""
    from app.pricing.pipeline import DEAL_EVIDENCE_COMPS

    thin = SoldDataset_thin()
    df = pd.DataFrame([_epsom(
        address="3 No Evidence Road", price_display="$2,400,000",
        price_numeric=2_400_000.0, cv_numeric=3_275_000.0,
        improvement_value_numeric=1_900_000.0, land_value_numeric=1_375_000.0)])
    out = run_pipeline(df, thin, None)

    if int(out.loc[0, "comps_used"] or 0) < DEAL_EVIDENCE_COMPS:
        assert not out.loc[0, "is_underpriced"], \
            "a discount off almost no sold evidence reached the deal feed"


def SoldDataset_thin():
    """Two sales only — below the evidence bar by design."""
    from app.pricing.comps import SoldDataset

    return SoldDataset(pd.DataFrame([{
        "address": f"{i} Lonely Road", "suburb": "Remuera", "district": "Auckland",
        "property_type": "House", "price_numeric": 3_100_000.0, "sale_price": 3_100_000.0,
        "cv_numeric": 3_200_000.0, "key_floor_area": 122.0, "key_land_area": 1053.0,
        "key_bedrooms": 3, "key_bathrooms": 2, "sold_date": "2026-06-01",
        "days_on_market": 30,
    } for i in range(2)]))


def SoldDataset_matching():
    """Sales the SIZE-CONTROLLED comp tier will actually match: same suburb, same
    floor area. A land-only CV can only be priced from size-controlled comps, so
    a sold set that differs on floor area sends the row down the
    insufficient-comps path and tests a different rule than the one intended."""
    from app.pricing.comps import SoldDataset

    return SoldDataset(pd.DataFrame([{
        "address": f"{i} Comparable Road", "suburb": "Remuera", "district": "Auckland",
        "property_type": "House", "price_numeric": 3_100_000.0 + i * 2_000,
        "sale_price": 3_100_000.0 + i * 2_000, "cv_numeric": 3_200_000.0,
        "key_floor_area": 122.0, "key_land_area": 1053.0,
        "key_bedrooms": 3, "key_bathrooms": 2, "sold_date": "2026-06-01",
        "days_on_market": 30,
    } for i in range(60)]))
