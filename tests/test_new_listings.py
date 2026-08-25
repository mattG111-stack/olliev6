"""New listings, daily, before the weekly file reaches them.

The weekly file is a snapshot: a home listed on Tuesday appears in it the
following Monday. An underpriced listing is under offer inside a week, so six
days late is the difference between seeing it and reading about it.

About a hundred new Auckland listings a day, which is what makes a daily sweep
cost three dollars a month rather than seventeen hundred — realestate.co.nz
filters to the last 24 hours SERVER-SIDE, so the run pays for the hundred new
ones and not the twelve thousand standing ones. Get that payload wrong and the
bill is the first thing that tells you, so it is pinned here.

The recorded actor shapes below come from each actor's published output schema.
No network, no token: the sweep takes a `runner` so the whole path — fetch,
normalise, dedupe, approve — runs against them.
"""
from __future__ import annotations

import json

import pytest

from app.models import BatchType, ImportBatch, PortalListing, PropertyForSale
from app.portals import listings as NL

# ---------------------------------------------------------------------------
# Recorded actor output
# ---------------------------------------------------------------------------
# fatihtahta/oneroof-nz-scraper, in its real shape: four levels deep, every
# level optional. Taken from the actor's published output schema.
ONEROOF_ITEM = {
    "record_id": "or-123",
    "location": {"full_address": "12 Cassino Terrace, Papakura",
                 "suburb": "Papakura", "locality": "Papakura",
                 "coordinates": {"latitude": -37.06, "longitude": 174.94}},
    "pricing": {"search_price_amount": 1_150_000,
                "display_price": "Enquiries over $1,150,000",
                "price_method": "Negotiation"},
    "property": {
        "bedrooms": 4, "bathrooms": 2, "parking": 2, "floor_area": "182m²",
        "property_data": {
            "Property Type": "House",
            "Floor Area": "182m²", "Land Area": "612m²",
            # The two that decide whether this row can be assessed for
            # subdivision at all.
            "Unitary Plan": "Residential - Mixed Housing Suburban Zone",
            "Title": "Freehold",
            "Decade Built": "1990s", "Condition": "Good",
        },
    },
    "metrics": {
        "valuation": {"rating_valuation": "$1,100,000",
                      "estimated_value": "$1,250,000",
                      "estimate_low": "$1,150,000", "estimate_high": "$1,350,000"},
        "market_activity": {"days_on_oneroof": "27",
                            "last_sold_price": "$860,000",
                            "last_sold_year": "2019"},
    },
    "source_specific": {"property": {"public_records": {
        "rateable_value": "$1,100,000",
        "land_value": "$700,000", "improvement_value": "$400,000"}}},
    "media": {"images": ["https://img.oneroof.co.nz/a.jpg"]},
    "listing": {"description": "Family home with a pool.",
                "additional_details": {"Listed on": {"iso_datetime": "2026-08-21"}}},
    "source_context": {"listing_url": "https://www.oneroof.co.nz/property/12-cassino-terrace",
                       "external_ids": {"property_id": "or-123"}},
}

REALESTATE_ITEM = {
    "entity": {"url": "https://www.realestate.co.nz/1234567",
               "description": "Sunny three bedroom."},
    "location": {"full_address": "8 Vittoria Terrace, Flat Bush",
                 "suburb": "Flat Bush", "district": "Manukau City",
                 "latitude": -36.99, "longitude": 174.91},
    "property": {"bedrooms": 3, "bathrooms": 2, "floor_area": 140.0,
                 "land_area": 405.0, "property_type": "House", "garages": 1},
    "valuation": {"capital_value": 980_000, "land_value": 560_000,
                  "improvement_value": 420_000},
    "media": {"photos": [{"base_url": "https://cdn.realestate.co.nz/p.jpg"}]},
    "pricing": {"price_text": "By negotiation"},
    "listing": {"listing_id": "re-777", "published_at": "2026-08-21T09:00:00"},
}


def runner_for(items_by_actor):
    """Stand in for run_actor, keyed by the actor name the sweep asks for."""
    calls = []

    def run(actor, payload, limit=None):
        calls.append((actor, payload, limit))
        return items_by_actor.get(actor, [])

    run.calls = calls
    return run


def _live_batch(db, *props):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="week.xlsx", is_active=True, status="published")
    db.add(b)
    db.flush()
    for address, suburb in props:
        db.add(PropertyForSale(import_batch_id=b.id, address=address,
                               suburb=suburb, property_type="House",
                               floor_area_m2=140.0, is_held=False))
    db.commit()
    return b


# ---------------------------------------------------------------------------
# The payloads — where the whole cost of this feature is decided
# ---------------------------------------------------------------------------
def test_realestate_asks_only_for_the_last_day():
    """The single most expensive line in this feature. Without the filter a
    daily run scrapes all twelve thousand standing Auckland listings instead of
    the hundred new ones — about $1,700 a month rather than $3."""
    p = NL._payload("realestate", hours=24)
    assert p["publication_date"] == "last_24_hours"
    assert p["sort_by"] == "latest"


def test_realestate_asks_for_the_valuations():
    """get_valuations defaults to FALSE. Forget it and every listing arrives
    with no CV, which is the number everything downstream is anchored to."""
    assert NL._payload("realestate")["get_valuations"] is True


@pytest.mark.parametrize("hours, window", [
    (24, "last_24_hours"), (48, "last_3_days"), (72, "last_3_days"),
    (120, "last_7_days"),
])
def test_a_longer_window_asks_for_a_longer_window(hours, window):
    """After an outage the sweep has to be able to catch up on missed days."""
    assert NL._payload("realestate", hours=hours)["publication_date"] == window


def test_oneroof_is_capped_because_it_cannot_filter_by_date():
    """It has no date filter, and `limit` defaults to 50,000 on this actor — so
    passing it is the only thing between a daily run and the whole country at
    half a cent a row."""
    p = NL._payload("oneroof", cap=300)
    assert p["limit"] == 300
    assert p["startUrls"] and "oneroof.co.nz" in p["startUrls"][0]


def test_trademe_is_not_swept():
    """Its actor returns no valuation of any kind, so a listing from it would
    arrive with no CV at all."""
    assert "trademe" not in NL.NEW_LISTING_SOURCES
    with pytest.raises(ValueError):
        NL._payload("trademe")


# ---------------------------------------------------------------------------
# Reading two very different shapes
# ---------------------------------------------------------------------------
def test_oneroof_carries_the_council_record():
    """Why OneRoof is worth $43 a month: the rating valuation AND the
    land/improvement split, which realestate does not publish and which the
    land-only-CV rule needs."""
    row = NL.to_listing("oneroof", ONEROOF_ITEM)
    assert row["cv_numeric"] == 1_100_000
    assert row["land_value_numeric"] == 700_000
    assert row["improvement_value_numeric"] == 400_000
    assert row["floor_area_m2"] == 182        # parsed out of "182m²"
    assert row["land_area_m2"] == 612
    assert (row["beds"], row["baths"], row["carspaces"]) == (4, 2, 2)
    assert row["suburb"] == "Papakura"


def test_realestate_carries_its_nested_shape():
    row = NL.to_listing("realestate", REALESTATE_ITEM)
    assert row["cv_numeric"] == 980_000
    assert row["floor_area_m2"] == 140 and row["land_area_m2"] == 405
    assert row["suburb"] == "Flat Bush"
    assert row["url"] == "https://www.realestate.co.nz/1234567"


def test_a_listing_with_no_address_is_dropped():
    """Without an address there is no way to tell whether we already hold the
    property, so it would arrive again every single day forever."""
    assert NL.to_listing("oneroof", {"priceValue": 900_000}) is None
    assert NL.to_listing("oneroof", {"address": "   "}) is None


def test_the_description_is_kept():
    """detect_pool() reads it, and it is the only place a portal says anything
    about condition."""
    assert "pool" in NL.to_listing("oneroof", ONEROOF_ITEM)["description"]


# ---------------------------------------------------------------------------
# The sweep, end to end, without a network
# ---------------------------------------------------------------------------
def test_a_sweep_records_what_it_found(db_session):
    _live_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM],
                      NL.LISTING_ACTORS["realestate"]: [REALESTATE_ITEM]})

    got = NL.sweep(db_session, runner=run)
    assert got["oneroof"]["new"] == 1
    assert got["realestate"]["new"] == 1
    assert db_session.query(PortalListing).count() == 2
    assert {r.status for r in db_session.query(PortalListing).all()} == {"pending"}


def test_nothing_goes_live_on_its_own(db_session):
    """The whole point of the review gate: a sweep must not create a listing a
    customer can see."""
    batch = _live_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM]})
    NL.sweep(db_session, sources=("oneroof",), runner=run)

    live = db_session.query(PropertyForSale).filter(
        PropertyForSale.import_batch_id == batch.id).count()
    assert live == 0, "a scraped listing reached the live batch unapproved"


def test_a_property_we_already_have_is_skipped(db_session):
    """The weekly file has it, so there is nothing to add."""
    _live_batch(db_session, ("12 Cassino Terrace", "Papakura"))
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM]})
    got = NL.sweep(db_session, sources=("oneroof",), runner=run)
    assert got["oneroof"] == {"found": 1, "new": 1 - 1, "skipped": 1}
    assert db_session.query(PortalListing).count() == 0


def test_the_same_listing_every_day_produces_one_row(db_session):
    """A listing sits on the market for six weeks. The sweep sees it forty-two
    times and it must appear once."""
    _live_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM]})
    for _ in range(5):
        NL.sweep(db_session, sources=("oneroof",), runner=run)
    assert db_session.query(PortalListing).count() == 1


def test_a_rejected_listing_does_not_come_back_tomorrow(db_session):
    """Deciding once has to mean once."""
    _live_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM]})
    NL.sweep(db_session, sources=("oneroof",), runner=run)
    row = NL.pending(db_session)[0]
    assert NL.reject(db_session, row.id)

    NL.sweep(db_session, sources=("oneroof",), runner=run)
    assert db_session.query(PortalListing).count() == 1
    assert not NL.pending(db_session)


def test_the_addresses_are_matched_however_each_portal_spells_them(db_session):
    """"3/107 Donovan Street" from us, "3 / 107 Donovan St" from a portal."""
    _live_batch(db_session, ("3/107 Donovan Street", "Blockhouse Bay"))
    import copy
    item = copy.deepcopy(ONEROOF_ITEM)
    item["location"]["full_address"] = "3 / 107 Donovan St, Blockhouse Bay"
    item["location"]["suburb"] = "Blockhouse Bay"
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [item]})
    got = NL.sweep(db_session, sources=("oneroof",), runner=run)
    assert got["oneroof"]["skipped"] == 1


def test_a_portal_having_a_bad_day_costs_only_that_portal(db_session):
    _live_batch(db_session)

    def run(actor, payload, limit=None):
        if actor == NL.LISTING_ACTORS["oneroof"]:
            raise RuntimeError("actor on fire")
        return [REALESTATE_ITEM]

    got = NL.sweep(db_session, runner=run)
    assert got["oneroof"]["found"] == 0
    assert got["realestate"]["new"] == 1


# ---------------------------------------------------------------------------
# Approving one
# ---------------------------------------------------------------------------
def test_approving_puts_it_in_the_live_batch_and_prices_it(db_session):
    batch = _live_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM]})
    NL.sweep(db_session, sources=("oneroof",), runner=run)
    row = NL.pending(db_session)[0]

    priced = []
    ok, why = NL.approve(db_session, row.id,
                         reprice=lambda db, pid: priced.append(pid))
    assert ok, why

    prop = db_session.get(PropertyForSale, row.property_id)
    assert prop.import_batch_id == batch.id
    assert prop.address == "12 Cassino Terrace, Papakura"
    assert prop.cv_numeric == 1_100_000
    assert prop.floor_area_m2 == 182
    assert prop.asking_price == 1_150_000
    assert priced == [prop.id], "an approved listing was not priced"


def test_an_approved_listing_carries_no_valuation_of_its_own(db_session):
    """Only facts cross over. Everything derived is derived by the pricing
    engine from those, through the same rules as the weekly file — a scraped
    listing does not get a shortcut."""
    _live_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM]})
    NL.sweep(db_session, sources=("oneroof",), runner=run)
    row = NL.pending(db_session)[0]
    NL.approve(db_session, row.id, reprice=lambda db, pid: None)

    prop = db_session.get(PropertyForSale, row.property_id)
    assert prop.fair_value is None and prop.opportunity_score_pct is None
    assert prop.is_underpriced in (None, False)


def test_a_portal_listing_now_arrives_with_its_zoning_and_title(db_session):
    """This test used to assert the opposite, and the assertion was correct
    about the actor it was written against.

    fatihtahta/oneroof-nz-scraper carries property_data["Unitary Plan"] and
    property_data["Title"] — the two fields that gate the subdivision engine.
    Without them every portal row read as "not subdividable" whatever its zone
    or its size. With them a portal listing is priced exactly like one from the
    weekly file."""
    _live_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM]})
    NL.sweep(db_session, sources=("oneroof",), runner=run)
    row = NL.pending(db_session)[0]
    NL.approve(db_session, row.id, reprice=lambda db, pid: None)

    prop = db_session.get(PropertyForSale, row.property_id)
    assert prop.zoning == "Residential - Mixed Housing Suburban Zone"
    assert prop.type_of_title == "Freehold"


def test_the_weekly_file_arriving_first_wins(db_session):
    """Between the sweep and the decision the real record may turn up, and it
    comes with the council data the portal has not got."""
    _live_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM]})
    NL.sweep(db_session, sources=("oneroof",), runner=run)
    row = NL.pending(db_session)[0]

    # The weekly upload lands, carrying the same property.
    batch = db_session.query(ImportBatch).first()
    db_session.add(PropertyForSale(
        import_batch_id=batch.id, address="12 Cassino Terrace",
        suburb="Papakura", property_type="House", floor_area_m2=182.0))
    db_session.commit()

    ok, why = NL.approve(db_session, row.id, reprice=lambda db, pid: None)
    assert not ok and "already" in why
    assert db_session.get(PortalListing, row.id).status == "superseded"


def test_a_listing_cannot_be_approved_twice(db_session):
    _live_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM]})
    NL.sweep(db_session, sources=("oneroof",), runner=run)
    row = NL.pending(db_session)[0]
    assert NL.approve(db_session, row.id, reprice=lambda db, pid: None)[0]
    ok, why = NL.approve(db_session, row.id, reprice=lambda db, pid: None)
    assert not ok and "approved" in why


def test_with_no_live_batch_nothing_can_be_approved(db_session):
    db_session.add(PortalListing(source="oneroof", address="1 Nowhere Road",
                                 address_key="1 nowhere road|", status="pending"))
    db_session.commit()
    row = NL.pending(db_session)[0]
    ok, why = NL.approve(db_session, row.id, reprice=lambda db, pid: None)
    assert not ok and "live batch" in why


def test_the_raw_answer_is_kept(db_session):
    """When a field turns out to be read wrong, the recorded answer is the only
    way to tell whether the portal changed or we did."""
    _live_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM]})
    NL.sweep(db_session, sources=("oneroof",), runner=run)
    row = NL.pending(db_session)[0]
    raw = json.loads(row.raw_json)
    assert raw["metrics"]["valuation"]["rating_valuation"] == "$1,100,000"


# ---------------------------------------------------------------------------
# The unattended daily run
# ---------------------------------------------------------------------------
def test_the_daily_pass_sweeps_before_it_enriches(monkeypatch):
    """New listings are the time-sensitive half, and a failure in either must
    not stop the other."""
    from app.portals import daily

    order = []
    monkeypatch.setattr(daily, "sweep_new_listings",
                        lambda: order.append("sweep") or {})
    monkeypatch.setattr(daily, "run_once", lambda: order.append("enrich"))

    # One turn of the loop body, without the thread or the sleeps.
    daily.sweep_new_listings()
    daily.run_once()
    assert order == ["sweep", "enrich"]


def test_a_failing_sweep_does_not_raise(monkeypatch):
    """This runs on a daemon thread with nobody watching. It reports and
    continues, or the whole daily job dies quietly on a bad night."""
    from app.portals import daily

    def boom(*_a, **_k):
        raise RuntimeError("apify is down")

    monkeypatch.setattr("app.portals.listings.sweep", boom)
    assert daily.sweep_new_listings() == {}


def test_the_sweep_is_off_unless_it_is_switched_on():
    """A background job that reaches the internet and spends money should be
    something someone turned on, not something a deploy starts."""
    from app.portals import daily

    assert hasattr(daily, "enabled")
    daily.start()          # no-op when PORTALS_DAILY is unset — must not raise


# ===========================================================================
# Sold — weekly, and the half that matters more
# ===========================================================================
"""A sale is not another listing.

For-sale gets a deal six days earlier. Sold data IS the engine — every comp,
every suburb sale/CV ratio, every valuation leans on it. Which cuts both ways:
a wrong asking price costs one listing, a wrong SALE price poisons a whole
suburb's $/m2 rate and ratio for everyone.

Weekly rather than daily because a week-old sale is still a comp where a
week-old listing is often already under offer — and because that cadence is
what makes it $13 a month instead of $90.
"""

import copy as _copy

ONEROOF_SOLD = _copy.deepcopy(ONEROOF_ITEM)
ONEROOF_SOLD["location"]["full_address"] = "9 Silvana Park Drive, Flat Bush"
ONEROOF_SOLD["location"]["suburb"] = "Flat Bush"
ONEROOF_SOLD["metrics"]["market_activity"].update(
    {"last_sold_price": "$1,240,000", "last_sold_year": "2026-08-15",
     "days_on_oneroof": "27"})
ONEROOF_SOLD["pricing"]["price_method"] = "Auction"


def sold_item(**changes):
    """A sold record with the market_activity fields overridden."""
    item = _copy.deepcopy(ONEROOF_SOLD)
    item["metrics"]["market_activity"].update(changes)
    return item


def _sold_batch(db, *sales):
    """An existing sold delivery, so dedupe and the price flag have a baseline."""
    from app.models import PropertySold

    b = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                    filename="sold-history.xlsx", is_active=True,
                    status="published")
    db.add(b)
    db.flush()
    for address, suburb, price, sold in sales:
        db.add(PropertySold(import_batch_id=b.id, address=address, suburb=suburb,
                            sale_price=price, sold_date=sold,
                            property_type="House"))
    db.commit()
    return b


# ---- the weekly payloads ---------------------------------------------------
def test_the_sold_sweep_asks_each_portal_for_sales():
    assert NL._sold_payload("realestate")["listing_type"] == "sold"
    assert NL._sold_payload("realestate")["sort_by"] == "latest-sale"
    assert "/sold/" in NL._sold_payload("oneroof")["startUrls"][0]


def test_the_sold_sweep_still_asks_for_the_valuations():
    """Sale price against CV is the metric. Without get_valuations there is no
    CV on the realestate record and the sale cannot be read as a percentage of
    anything. OneRoof's actor returns the council record unconditionally."""
    assert NL._sold_payload("realestate")["get_valuations"] is True
    assert NL._sold_payload("oneroof")["limit"] >= 500


def test_the_sold_cap_is_a_weeks_worth_not_a_days():
    """Neither portal can filter sold records by date server-side, so the cap is
    the only thing between a weekly run and every sale ever recorded."""
    assert NL.WEEKLY_SOLD_CAP >= 500
    assert NL._sold_payload("oneroof", cap=1000)["limit"] == 1000


# ---- reading a sale --------------------------------------------------------
def test_a_sale_carries_its_price_date_and_method():
    row = NL.to_listing("oneroof", ONEROOF_SOLD, kind="sold")
    assert row["kind"] == "sold"
    assert row["sale_price"] == 1_240_000
    assert row["sold_date"] == "2026-08-15"
    assert row["sale_method"] == "Auction"
    assert row["days_on_market"] == 27
    assert row["cv_numeric"] == 1_100_000        # still the council record


def test_a_sale_with_no_price_or_no_date_is_not_a_comp():
    """It cannot be placed in time or compared to anything, so it is an address
    and nothing else."""
    assert NL.to_listing("oneroof", sold_item(last_sold_price=None),
                         kind="sold") is None
    assert NL.to_listing("oneroof", sold_item(last_sold_year=None),
                         kind="sold") is None


# ---- the sanity check ------------------------------------------------------
def test_a_missing_digit_is_flagged_against_the_suburbs_own_median(db_session):
    """$124k where the suburb runs $1.2M. Not rejected — an odd sale is
    sometimes real — but it is the row to read before approving."""
    _sold_batch(db_session, *[(f"{i} Silvana Park Drive", "Flat Bush",
                               1_200_000, "2026-05-01") for i in range(6)])
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [sold_item(last_sold_price=124_000)]})
    NL.sweep_sold(db_session, sources=("oneroof",), runner=run)
    row = NL.pending(db_session, kind="sold")[0]
    assert row.price_flag and "digit" in row.price_flag
    assert row.status == "pending", "a flag is a warning, not a rejection"


def test_an_extra_digit_is_flagged_too(db_session):
    _sold_batch(db_session, *[(f"{i} Silvana Park Drive", "Flat Bush",
                               1_200_000, "2026-05-01") for i in range(6)])
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [sold_item(last_sold_price=12_400_000)]})
    NL.sweep_sold(db_session, sources=("oneroof",), runner=run)
    assert "digit" in NL.pending(db_session, kind="sold")[0].price_flag


def test_an_ordinary_sale_is_not_flagged(db_session):
    _sold_batch(db_session, *[(f"{i} Silvana Park Drive", "Flat Bush",
                               1_200_000, "2026-05-01") for i in range(6)])
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_SOLD]})
    NL.sweep_sold(db_session, sources=("oneroof",), runner=run)
    assert NL.pending(db_session, kind="sold")[0].price_flag is None


def test_a_suburb_with_too_few_sales_has_no_opinion(db_session):
    """An opinion formed from four sales is not worth flagging a fifth over."""
    _sold_batch(db_session, ("1 Somewhere Road", "Flat Bush", 1_200_000, "2026-05-01"))
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [sold_item(last_sold_price=124_000)]})
    NL.sweep_sold(db_session, sources=("oneroof",), runner=run)
    assert NL.pending(db_session, kind="sold")[0].price_flag is None


# ---- dedupe, which is different for sales ----------------------------------
def test_a_sale_we_already_hold_is_skipped(db_session):
    _sold_batch(db_session, ("9 Silvana Park Drive", "Flat Bush",
                             1_240_000, "2026-08-15"))
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_SOLD]})
    got = NL.sweep_sold(db_session, sources=("oneroof",), runner=run)
    assert got["oneroof"]["skipped"] == 1
    assert not NL.pending(db_session, kind="sold")


def test_the_same_house_selling_twice_is_two_sales(db_session):
    """Address alone would be wrong: a 2019 sale must not stop us recording the
    2026 one. The month is what separates them."""
    _sold_batch(db_session, ("9 Silvana Park Drive", "Flat Bush",
                             800_000, "2019-03-10"))
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_SOLD]})
    got = NL.sweep_sold(db_session, sources=("oneroof",), runner=run)
    assert got["oneroof"]["new"] == 1


def test_a_house_can_be_for_sale_and_sold_at_once(db_session):
    """It was listed, then it sold. Both records are true, and the for-sale
    dedupe must not swallow the sale."""
    _live_batch(db_session, ("9 Silvana Park Drive", "Flat Bush"))
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_SOLD]})
    got = NL.sweep_sold(db_session, sources=("oneroof",), runner=run)
    assert got["oneroof"]["new"] == 1


def test_the_weekly_sweep_run_twice_records_one_sale(db_session):
    _sold_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_SOLD]})
    for _ in range(3):
        NL.sweep_sold(db_session, sources=("oneroof",), runner=run)
    assert len(NL.pending(db_session, kind="sold")) == 1


# ---- approving a sale ------------------------------------------------------
def test_approving_a_sale_puts_it_in_the_sold_pool(db_session):
    from app.models import PropertySold

    _sold_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_SOLD]})
    NL.sweep_sold(db_session, sources=("oneroof",), runner=run)
    row = NL.pending(db_session, kind="sold")[0]

    ok, why = NL.approve(db_session, row.id)
    assert ok, why
    sale = db_session.get(PropertySold, row.property_id)
    assert sale.sale_price == 1_240_000
    assert sale.sold_date == "2026-08-15"
    assert sale.cv_numeric == 1_100_000
    assert sale.suburb == "Flat Bush"


def test_a_portal_sale_joins_the_pool_rather_than_replacing_it(db_session):
    """A sold batch is a DELIVERY, not the dataset. Portal sales land in their
    own delivery and every reader goes through sold_batch_ids, so the uploaded
    history stays exactly where it was."""
    from app.ingest import sold_batch_ids
    from app.models import PropertySold

    _sold_batch(db_session, ("1 Old Road", "Flat Bush", 900_000, "2025-01-01"))
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_SOLD]})
    NL.sweep_sold(db_session, sources=("oneroof",), runner=run)
    NL.approve(db_session, NL.pending(db_session, kind="sold")[0].id)

    live = sold_batch_ids(db_session, "Auckland")
    assert len(live) >= 2, "the portal delivery did not join the pool"
    total = (db_session.query(PropertySold)
             .filter(PropertySold.import_batch_id.in_(live)).count())
    assert total == 2, "an existing sale was lost"


def test_approving_a_sale_does_not_try_to_price_it(db_session):
    """A sale is not valued. It is what everything else is valued against."""
    _sold_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_SOLD]})
    NL.sweep_sold(db_session, sources=("oneroof",), runner=run)
    row = NL.pending(db_session, kind="sold")[0]

    def must_not_run(*_a, **_k):
        raise AssertionError("a sale was sent to the pricing engine")

    ok, _ = NL.approve(db_session, row.id, reprice=must_not_run)
    assert ok


def test_pending_can_be_asked_for_one_kind(db_session):
    _live_batch(db_session)
    _sold_batch(db_session)
    NL.sweep(db_session, sources=("oneroof",),
             runner=runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM]}))
    NL.sweep_sold(db_session, sources=("oneroof",),
                  runner=runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_SOLD]}))

    assert len(NL.pending(db_session)) == 2
    assert len(NL.pending(db_session, kind="sold")) == 1
    assert len(NL.pending(db_session, kind="for_sale")) == 1


def test_sales_are_swept_weekly_not_nightly(monkeypatch):
    """The cadence is the cost. A daily sold sweep is ~$90 a month for records
    that do not change; weekly is ~$13 for the same ones."""
    from app.portals import daily

    assert daily.SOLD_EVERY_DAYS == 7

    passes = {"listings": 0, "sold": 0}
    monkeypatch.setattr(daily, "sweep_new_listings",
                        lambda: passes.__setitem__("listings", passes["listings"] + 1) or {})
    monkeypatch.setattr(daily, "sweep_sold_listings",
                        lambda: passes.__setitem__("sold", passes["sold"] + 1) or {})

    # Fourteen turns of the loop body, without the thread or the sleeps.
    for day in range(14):
        daily.sweep_new_listings()
        if day % daily.SOLD_EVERY_DAYS == 0:
            daily.sweep_sold_listings()
    assert passes == {"listings": 14, "sold": 2}


def test_a_failing_sold_sweep_does_not_raise(monkeypatch):
    from app.portals import daily

    def boom(*_a, **_k):
        raise RuntimeError("apify is down")

    monkeypatch.setattr("app.portals.listings.sweep_sold", boom)
    assert daily.sweep_sold_listings() == {}


def _admin(db):
    """A real admin row — app_settings.updated_by is a foreign key, so a stub
    with a made-up id fails the constraint rather than the assertion."""
    from app.models import User

    u = db.query(User).first()
    if u is None:
        u = User(email="admin@test.co.nz", password_hash="x", role="admin")
        db.add(u)
        db.commit()
    return u


# ===========================================================================
# The token: set it in the app, test it, run it
# ===========================================================================
"""Telling someone to set an environment variable and redeploy is not a
feature. The token goes in the admin panel, is checked against Apify before it
is stored, and is encrypted at rest with the same key as the assistant's —
because a token typed into a browser must not be readable by anyone who gets a
look at the database.
"""


def test_the_environment_wins_over_the_panel(monkeypatch, db_session):
    """A value in Railway is what a deploy is reproducible from. Both are
    useful; only one can be authoritative, and it has to be the one that
    survives a redeploy."""
    from app.assistant import keys
    from app.models import APIFY_TOKEN
    from app.portals import apify
    from app.settings_store import put as put_setting

    put_setting(db_session, APIFY_TOKEN, keys.encrypt("from-the-panel"))
    monkeypatch.setattr(apify.settings, "apify_token", "from-the-env")
    assert apify.token(db_session) == "from-the-env"


def test_a_token_saved_in_the_panel_is_used_when_the_environment_is_empty(db_session):
    from app.assistant import keys
    from app.models import APIFY_TOKEN
    from app.portals import apify
    from app.settings_store import put as put_setting

    put_setting(db_session, APIFY_TOKEN, keys.encrypt("from-the-panel"))
    assert apify.token(db_session) == "from-the-panel"


def test_the_token_is_encrypted_at_rest(db_session):
    """Readable from the app, not from the table."""
    from app.assistant import keys
    from app.models import APIFY_TOKEN
    from app.settings_store import get as get_setting
    from app.settings_store import put as put_setting

    put_setting(db_session, APIFY_TOKEN, keys.encrypt("apify_api_secret123"))
    stored = get_setting(db_session, APIFY_TOKEN)
    assert "apify_api_secret123" not in (stored or "")
    assert keys.decrypt(stored) == "apify_api_secret123"


def test_no_token_anywhere_reads_as_not_configured(monkeypatch, db_session):
    from app.portals import apify

    monkeypatch.setattr(apify.settings, "apify_token", "")
    assert apify.token(db_session) == ""
    assert apify.configured(db_session) is False


def test_the_token_lookup_never_raises(monkeypatch):
    """Called on the unattended path with no session. A settings store that is
    unreachable must read as "no token", not take the sweep down."""
    from app.portals import apify

    monkeypatch.setattr(apify.settings, "apify_token", "")
    assert apify.token(None) == ""


def test_a_dead_token_is_reported_not_stored(monkeypatch, db_session):
    """The failure people actually hit is a real token on an account with no
    credit. Storing one we already know is dead only moves the discovery to the
    first sweep."""
    from app.models import APIFY_TOKEN
    from app.routers import release
    from app.settings_store import get as get_setting

    monkeypatch.setattr("app.portals.apify.check",
                        lambda t: (False, "Apify rejected this token"))
    admin = _admin(db_session)
    out = release.save_apify_token(release.ApifyTokenIn(token="bad"),
                                   admin=admin, db=db_session)
    assert out.ok is False and "rejected" in out.message
    assert get_setting(db_session, APIFY_TOKEN) is None, "a dead token was saved"


def test_a_working_token_is_saved_and_never_echoed(monkeypatch, db_session):
    from app.routers import release

    monkeypatch.setattr("app.portals.apify.check",
                        lambda t: (True, "Connected to matt (free)"))
    admin = _admin(db_session)
    out = release.save_apify_token(
        release.ApifyTokenIn(token="apify_api_abcdefgh1234"), admin=admin,
        db=db_session)
    assert out.configured and out.ok
    assert out.last_four == "1234"
    # The whole token must never come back out of the API.
    assert "apify_api_abcdefgh1234" not in out.model_dump_json()


def test_an_empty_token_removes_it(monkeypatch, db_session):
    from app.assistant import keys
    from app.models import APIFY_TOKEN
    from app.routers import release
    from app.settings_store import get as get_setting
    from app.settings_store import put as put_setting

    put_setting(db_session, APIFY_TOKEN, keys.encrypt("old"))
    admin = _admin(db_session)
    out = release.save_apify_token(release.ApifyTokenIn(token="  "),
                                   admin=admin, db=db_session)
    assert out.configured is False
    assert get_setting(db_session, APIFY_TOKEN) is None


def test_the_status_says_where_the_token_came_from(monkeypatch, db_session):
    """Because the environment wins, and someone changing the wrong one and
    seeing nothing happen is a bad afternoon."""
    from app.assistant import keys
    from app.models import APIFY_TOKEN
    from app.routers import release
    from app.settings_store import put as put_setting

    admin = _admin(db_session)
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.setattr("app.portals.apify.settings.apify_token", "")
    put_setting(db_session, APIFY_TOKEN, keys.encrypt("panel-token"))
    assert release.apify_status(admin=admin, db=db_session).source == "panel"

    monkeypatch.setenv("APIFY_TOKEN", "env-token")
    monkeypatch.setattr("app.portals.apify.settings.apify_token", "env-token")
    st = release.apify_status(admin=admin, db=db_session)
    assert st.source == "environment" and st.locked is True


def test_a_sweep_with_no_token_says_so(monkeypatch, db_session):
    """Rather than reporting "0 found" and sending someone hunting a scraper
    bug that is really a missing token."""
    from fastapi import HTTPException

    from app.routers import release

    monkeypatch.setattr("app.portals.apify.token", lambda db=None: "")
    admin = _admin(db_session)
    with pytest.raises(HTTPException) as e:
        release.sweep_new_listings(admin=admin, db=db_session)
    assert e.value.status_code == 400
    assert "token" in e.value.detail.lower()


# ===========================================================================
# The sweep is a JOB, not a request
# ===========================================================================
"""500 with no response body, on the first press of Check now.

An Apify actor takes tens of seconds to a few minutes, and the sweep asks TWO of
them — up to six minutes with the default 180-second timeouts. Held open inside
the HTTP request, that is cut off by the platform's proxy long before the answer
exists, and the browser gets a 500 with nothing in it. The sweep may have worked
perfectly and nobody would ever know.

The portals button has always been a background job for exactly this reason.
This one was written synchronously and hit it on the first press.
"""


def test_the_sweep_returns_a_job_and_does_not_do_the_work(monkeypatch, db_session):
    """The request must come back at once. If it waits for Apify it is cut off
    by the proxy and the answer is lost, however well the sweep went."""
    from app.routers import release

    monkeypatch.setattr("app.portals.apify.token", lambda db=None: "a-token")
    started = []
    monkeypatch.setattr(release.threading, "Thread",
                        lambda **kw: type("T", (), {
                            "start": lambda _self: started.append(kw)})())

    out = release.sweep_new_listings(admin=_admin(db_session), db=db_session)
    assert out.job_id, "no job to poll"
    assert started, "the work did not go to a thread"
    assert started[0]["kwargs"]["kind"] == "for_sale"


def test_the_sold_sweep_is_a_job_too(monkeypatch, db_session):
    """It asks for a thousand rows, so it takes longer, not less."""
    from app.routers import release

    monkeypatch.setattr("app.portals.apify.token", lambda db=None: "a-token")
    started = []
    monkeypatch.setattr(release.threading, "Thread",
                        lambda **kw: type("T", (), {
                            "start": lambda _self: started.append(kw)})())

    out = release.sweep_sold_endpoint(admin=_admin(db_session), db=db_session)
    assert out.job_id
    assert started[0]["kwargs"]["kind"] == "sold"


def test_a_sweep_job_belongs_to_no_batch(db_session):
    """It is looking for properties that are not in a batch yet. batch_id is a
    NULLABLE foreign key, so None is the only correct value — 0 passes on
    SQLite and violates the constraint on Postgres, which is the worst
    possible combination."""
    from app.models import IngestJob
    from app.staged_stages import create_stage_job

    job = create_stage_job(db_session, stage="listings", batch_id=None,
                           region="Auckland", uploaded_by_id=None)
    assert job.batch_id is None
    assert db_session.get(IngestJob, job.id).filename == "listings"


def test_the_job_records_what_the_sweep_found(monkeypatch, db_session):
    """The job row is the only place anyone can see what happened, because the
    request answered before the work started."""
    from app.models import IngestJob
    from app.routers import release
    from app.staged_stages import create_stage_job

    job = create_stage_job(db_session, stage="listings", batch_id=None,
                           region="Auckland", uploaded_by_id=None)
    monkeypatch.setattr("app.portals.listings.sweep",
                        lambda db, **kw: {"oneroof": {"found": 9, "new": 4,
                                                      "skipped": 5}})
    release._run_sweep_job(job.id, kind="for_sale", hours=24, cap=10)

    db_session.expire_all()
    row = db_session.get(IngestJob, job.id)
    assert row.status == "completed"
    assert row.rows_total == 9 and row.rows_inserted == 4
    assert "oneroof" in (row.stage or "")


def test_a_thread_that_dies_says_so_on_the_job(monkeypatch, db_session):
    """A background thread that raises takes its reason with it. Without this
    the job sits at "running" forever and the panel spins."""
    from app.models import IngestJob
    from app.routers import release
    from app.staged_stages import create_stage_job

    job = create_stage_job(db_session, stage="listings", batch_id=None,
                           region="Auckland", uploaded_by_id=None)

    def boom(db, **kw):
        raise RuntimeError("apify said no")

    monkeypatch.setattr("app.portals.listings.sweep", boom)
    release._run_sweep_job(job.id, kind="for_sale", hours=24, cap=10)

    db_session.expire_all()
    row = db_session.get(IngestJob, job.id)
    assert row.status == "failed"
    assert "apify said no" in (row.error_message or "")


def test_the_summary_fits_the_column(monkeypatch, db_session):
    """`stage` is String(64). A summary longer than that is a write that fails
    inside a background thread — the worst place for one."""
    from app.models import IngestJob
    from app.routers import release
    from app.staged_stages import create_stage_job

    job = create_stage_job(db_session, stage="listings", batch_id=None,
                           region="Auckland", uploaded_by_id=None)
    monkeypatch.setattr(
        "app.portals.listings.sweep",
        lambda db, **kw: {f"source-with-a-very-long-name-{i}":
                          {"found": 100, "new": 50} for i in range(8)})
    release._run_sweep_job(job.id, kind="for_sale", hours=24, cap=10)

    db_session.expire_all()
    row = db_session.get(IngestJob, job.id)
    assert row.status == "completed"
    assert len(row.stage or "") <= 64


# ===========================================================================
# The richer OneRoof actor — what changes because of it
# ===========================================================================
"""I said no portal carries zoning or title. That was true of the actor I had
checked and wrong as a claim about portals.

fatihtahta/oneroof-nz-scraper carries both, four levels down:

    property.property_data["Unitary Plan"]   the zone
    property.property_data["Title"]          freehold / cross-lease / unit title

They are the two fields that gate the subdivision engine, so their absence was
not a missing tile — it was every portal listing reading as "not subdividable"
regardless of its zone or its size. At the same price per thousand.
"""


def test_it_supplies_every_field_the_pricing_engine_reads():
    """Twenty-one fields are read off a row in pricing/pipeline.py. The previous
    actor supplied seventeen. This one supplies all of them, which is the
    difference between a portal listing that is priced like any other and one
    that is priced with holes in it."""
    row = NL.to_listing("oneroof", ONEROOF_ITEM)
    for field in ("address", "suburb", "district", "property_type",
                  "price_numeric", "price_display", "cv_numeric",
                  "land_value_numeric", "improvement_value_numeric",
                  "floor_area_m2", "land_area_m2", "beds", "baths",
                  "carspaces", "zoning", "type_of_title", "building_age"):
        assert row.get(field) not in (None, ""), f"{field} is missing"


def test_the_zone_comes_through_under_the_name_oneroof_uses_for_it():
    """"Unitary Plan", not "zoning". Nothing else on the record says it."""
    row = NL.to_listing("oneroof", ONEROOF_ITEM)
    assert row["zoning"] == "Residential - Mixed Housing Suburban Zone"
    assert row["type_of_title"] == "Freehold"


def test_the_council_split_comes_from_public_records():
    """The rating valuation appears twice — under metrics as the headline and
    under public_records with the split it is made of. The split is what the
    land-only-CV rule needs."""
    row = NL.to_listing("oneroof", ONEROOF_ITEM)
    assert row["cv_numeric"] == 1_100_000
    assert row["land_value_numeric"] == 700_000
    assert row["improvement_value_numeric"] == 400_000


def test_their_estimate_is_kept_as_theirs():
    """OneRoof's AVM, with its band. Stored in its own field, shown as theirs,
    and never an input to our valuation — a portal's opinion of a price is not
    evidence about that price."""
    row = NL.to_listing("oneroof", ONEROOF_ITEM)
    assert row["estimate"] == 1_250_000
    assert row["estimate_low"] == 1_150_000
    assert row["estimate_high"] == 1_350_000
    # And it is NOT what we would price from.
    assert row["cv_numeric"] != row["estimate"]


def test_a_listing_with_no_council_record_still_reads():
    """Every level of this actor's output is optional — a property with no
    public records simply has no key. Four levels of chained .get() is where an
    AttributeError inside a background thread comes from."""
    bare = {"location": {"full_address": "1 Bare Road", "suburb": "Papakura"},
            "property": {"bedrooms": 3}}
    row = NL.to_listing("oneroof", bare)
    assert row is not None
    assert row["address"] == "1 Bare Road"
    assert row["cv_numeric"] is None and row["zoning"] is None


def test_a_completely_empty_item_does_not_raise():
    assert NL.to_listing("oneroof", {}) is None
    assert NL.to_listing("oneroof", {"location": {}}) is None


def test_the_actor_is_the_one_that_carries_the_zone():
    """A swap back to the leaner actor would pass every other test in this file
    and quietly stop collecting zoning and title."""
    assert NL.LISTING_ACTORS["oneroof"] == "fatihtahta/oneroof-nz-scraper"


def test_the_zone_and_title_reach_the_priced_row(db_session):
    """Carrying them into PortalListing is only half of it — they have to cross
    to PropertyForSale, because that is what the subdivision engine reads."""
    _live_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM]})
    NL.sweep(db_session, sources=("oneroof",), runner=run)
    row = NL.pending(db_session)[0]
    NL.approve(db_session, row.id, reprice=lambda db, pid: None)

    prop = db_session.get(PropertyForSale, row.property_id)
    assert prop.zoning == "Residential - Mixed Housing Suburban Zone"
    assert prop.type_of_title == "Freehold"


def test_a_portal_listing_can_now_be_assessed_for_subdivision(db_session):
    """The end of it. With a zone and a freehold title the subdivision engine
    can answer, where before it returned unknown_title on every portal row."""
    from app.pricing.subdivision import compute

    _live_batch(db_session)
    run = runner_for({NL.LISTING_ACTORS["oneroof"]: [ONEROOF_ITEM]})
    NL.sweep(db_session, sources=("oneroof",), runner=run)
    row = NL.pending(db_session)[0]
    NL.approve(db_session, row.id, reprice=lambda db, pid: None)
    prop = db_session.get(PropertyForSale, row.property_id)

    verdict = compute(zone=prop.zoning, land_area=700, buy_price=900_000,
                      section_rate=1500, title_type=prop.type_of_title,
                      property_type=prop.property_type, cv=prop.cv_numeric,
                      beds=4, baths=2, floor_area=prop.floor_area_m2)
    assert verdict.section_value_method not in ("unknown_title", "excluded_by_title")
