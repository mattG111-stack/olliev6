"""Finding development land is the point. A missing price cannot hide it.

    "We should still flag all subdividable houses that's our super power a user
     needs to be able to filter by zoning they are looking for"

One flag was doing two jobs and only ever answering one of them. `is_subdividable`
means "worth subdividing" — the site splits AND the split makes money at what you
would pay for it — which is a deal signal and needs a price. Four listings in
five are sold by auction, tender or negotiation and have no price, so the answer
for every one of them was False.

Not "unrated". False. A 1,200 m² freehold site in Mixed Housing Urban going to
auction was invisible to somebody searching for development land — which is the
thing they came here for. It was hidden by the fix that stopped us inventing its
price, and that is the fix creating a worse problem than the one it solved.

So the two questions are separated:

    can_subdivide     the zone allows it, the title allows it, the land is big
                      enough, and the arithmetic leaves at least one extra lot.
                      No price anywhere in that sentence.

    is_subdividable   AND it makes money. Still the deal signal, still needs a
                      price, still drives the buy score.

A developer can decide for themselves what a site is worth. What they cannot do
is judge a site we never showed them.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.ingest import ingest_for_sale, ingest_sold
from app.models import ImportBatch, PropertyForSale
from app.pricing import subdivision as SD

MHU = "Residential - Mixed Housing Urban Zone"
MHS = "Residential - Mixed Housing Suburban Zone"
SINGLE = "Residential - Single House Zone"


# ---- the rule itself --------------------------------------------------------
def _site(**over):
    args = dict(zone=MHU, land_area=1200, property_type="House",
                title_type="Freehold", beds=3, baths=1, floor_area=100,
                improvement_value=200_000, land_value=500_000, buy_price=None,
                cv=700_000, section_rate=700.0, address="9 Example Road")
    args.update(over)
    return SD.compute(**args)


def test_a_site_with_no_price_can_still_be_subdivided():
    """THE ONE THAT MATTERS. Nothing about splitting land needs a vendor to have
    named a price."""
    sd = _site()
    assert sd.can_subdivide is True
    assert sd.max_addl_lots == 2.0


def test_but_it_is_not_called_worth_subdividing():
    """Because that DOES need a price — the profit is what you'd pay against
    what the sections fetch — and there is no price to work it from."""
    sd = _site()
    assert sd.is_subdividable is False
    assert sd.subdivision_profit is None


def test_a_site_that_splits_but_loses_money_is_still_findable():
    """The other half of the separation, and the reason it is two flags rather
    than one relaxed one. A site can be real development land and a bad buy at
    today's price; the developer is entitled to see it and say so."""
    sd = _site(buy_price=5_000_000.0)
    assert sd.can_subdivide is True
    assert sd.is_subdividable is False
    assert sd.subdivision_profit is not None and sd.subdivision_profit < 0


def test_and_one_that_pays_is_both():
    sd = _site(buy_price=640_000.0, section_rate=2_400.0)
    assert sd.can_subdivide is True
    assert sd.is_subdividable is True


# ---- what still cannot be subdivided, price or no price ---------------------
@pytest.mark.parametrize("label,over", [
    ("single-house zone", dict(zone=SINGLE)),
    ("unit title", dict(title_type="Unit Title")),
    ("cross lease", dict(title_type="Cross Lease")),
    ("apartment", dict(property_type="Apartment")),
    ("too small", dict(land_area=400)),
    ("no land area", dict(land_area=None)),
])
def test_the_eligibility_rules_are_untouched(label, over):
    """Relaxing the FLAG must not relax the RULES. Every one of these is a
    reason a site genuinely cannot be split, and none of them is about money."""
    assert _site(**over).can_subdivide is False, label


def test_a_corrupt_land_area_is_not_a_fact_either():
    """The one non-price reason feasibility is withheld: when the council
    figures say the land is impossibly cheap per m², the size the lot count was
    built on is wrong, so the lot count is not a fact about the site."""
    sd = _site(land_area=21_109, cv=1_400_000)
    assert sd.can_subdivide is False


# ---- and it reaches the page ------------------------------------------------
def _sold_frame():
    return pd.DataFrame([{
        "address": f"{i} Sold Street", "suburb": "Papakura", "district": "Papakura",
        "region": "Auckland", "property_type": "House", "key_bedrooms": 3,
        "key_bathrooms": 1, "key_floor_area": f"{100 + i} sqm",
        "key_land_area": f"{900 + i} sqm", "cv_numeric": 700_000,
        "price_numeric": 720_000 + i * 5_000, "sale_price": 720_000 + i * 5_000,
        "land_value_numeric": 500_000, "improvement_value_numeric": 200_000,
        "type_of_title": "Freehold", "sold_date": "2026-06-01",
    } for i in range(12)])


def _row(slug, **over):
    row = dict(address=f"{slug} Road", suburb="Papakura", district="Papakura",
               region="Auckland", property_type="House", slug_id=slug,
               url=f"https://oneroof.co.nz/{slug}", price_display="$700,000",
               price_numeric=700_000, sale_method="auction", zoning=MHU,
               cv_numeric=700_000, land_value_numeric=500_000,
               improvement_value_numeric=200_000, key_bedrooms=3,
               key_bathrooms=1, key_carspaces=1, key_floor_area=100,
               key_land_area=1200, type_of_title="Freehold")
    row.update(over)
    return row


def _load(db, rows):
    ingest_sold(db, _sold_frame(), "sold.csv", region="Auckland", publish=True)
    ingest_for_sale(db, pd.DataFrame(rows), _sold_frame(), "live.csv",
                    region="Auckland", publish=True)
    batch = (db.query(ImportBatch)
             .filter(ImportBatch.batch_type == "for_sale",
                     ImportBatch.is_active.is_(True))
             .order_by(ImportBatch.id.desc()).first())
    return batch.id


def test_an_auction_site_reaches_the_subdividable_page(db_session):
    """Asked at the page, because that is where it went missing."""
    from app.routers.properties import _filtered_query

    batch = _load(db_session, [_row("big-auction-site"),
                               _row("small-house", key_land_area=400)])
    found = _filtered_query(db_session, batch, subdividable=True).all()
    assert [p.slug_id for p in found] == ["big-auction-site"]
    assert found[0].asking_price is None, "it is still a listing with no price"
    assert found[0].is_subdividable is False, "and still not called a deal"


def test_a_developer_can_filter_by_the_zone_they_work_in(db_session):
    from app.routers.properties import _filtered_query

    batch = _load(db_session, [_row("urban"), _row("suburban", zoning=MHS)])
    got = _filtered_query(db_session, batch, zoning=MHU).all()
    assert [p.slug_id for p in got] == ["urban"]

    both = _filtered_query(db_session, batch, zoning=f"{MHU},{MHS}").all()
    assert sorted(p.slug_id for p in both) == ["suburban", "urban"]


def test_ollies_hunt_finds_it_too(db_session):
    """The same question asked a different way, in a different file.

    Somebody who tells Ollie they are looking for development land is asking
    what the Subdividable page asks, and preferences.py answered it with the
    profit flag — so the 138 sites with no advertised price were missing there
    as well. One rule can be fixed in three places and still be wrong in the
    fourth; the only way to know is to ask each of them.
    """
    from app.routers.preferences import _matching_query

    batch = _load(db_session, [_row("big-auction-site"),
                               _row("small-house", key_land_area=400)])
    prefs = {"suburbs": [], "districts": [], "min_price": None,
             "max_price": None, "min_beds": None, "goals": ["subdividable"]}
    got = _matching_query(db_session, batch, prefs).all()
    assert [p.slug_id for p in got] == ["big-auction-site"]


def test_a_saved_budget_still_finds_development_land(db_session):
    """And the budget on that same query, which read the asking price alone —
    so a customer with a budget saw none of it."""
    from app.routers.preferences import _matching_query

    batch = _load(db_session, [_row("big-auction-site")])
    site = _filtered_site(db_session, "big-auction-site")
    assert site.asking_price is None
    value = site.fair_value or site.market_value

    prefs = {"suburbs": [], "districts": [], "min_price": None,
             "max_price": value + 250_000, "min_beds": None,
             "goals": ["subdividable"]}
    assert [p.slug_id for p in _matching_query(db_session, batch, prefs).all()] \
        == ["big-auction-site"]


def test_worth_subdividing_always_implies_it_can_be_subdivided():
    """The invariant between the two flags, which nothing but arithmetic
    currently keeps. A profit is only ever computed on the feasible path, so
    "worth splitting but not splittable" is not a state that can exist — and a
    row in it would show a subdivision deal on a site the filter cannot find."""
    for over in (dict(buy_price=640_000.0, section_rate=2_400.0),   # profitable
                 dict(buy_price=5_000_000.0),                       # loses money
                 dict(buy_price=None),                              # no price
                 dict(zone=SINGLE), dict(land_area=400)):           # not eligible
        sd = _site(**over)
        assert not (sd.is_subdividable and not sd.can_subdivide), over


def _filtered_site(db, slug):
    return db.query(PropertyForSale).filter(PropertyForSale.slug_id == slug).one()


def test_the_zone_list_says_what_is_in_each(db_session):
    """The picker is built from the data, so it cannot be typed wrong and it
    doubles as the answer to "what is in here"."""
    from app.routers.properties import zones

    _load(db_session, [_row("urban-big"), _row("urban-small", key_land_area=400),
                       _row("suburban", zoning=MHS)])
    out = {z.zoning: z for z in zones(region="Auckland", db=db_session)}
    assert out[MHU].live == 2
    assert out[MHU].can_subdivide == 1
    assert out[MHS].live == 1
