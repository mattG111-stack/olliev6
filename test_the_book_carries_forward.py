"""A weekly file is not the market.

    "so i loaded the lastest data and instead of adding it too what was there it
     now only shows the new"

    "the new data just goes on top until we have classed it as sold — through
     the new feature we did today"

Every for-sale upload archived the batch before it, so the newest file became
the whole truth and anything it did not mention vanished from the site as though
it had sold. That is only correct if the file is a complete snapshot of the
market every single time — and a scrape never is. A rate limit, a changed page,
a portal having a bad night, and a house that is still very much for sale is
simply absent. A short file did not shrink the book, it replaced it.

So listings accumulate, and one leaves only when its ADVERTISEMENT is gone —
which app/portals/delisted.py finds out by opening it. The spreadsheet no longer
gets a vote on whether a house exists.

The dangerous part of this is not the carrying, it is what gets LOST while
carrying. A stored row and the scrape row it came from are different shapes:
beds arrives as key_bedrooms, the main photograph as image_1_url, the asking
price as price_numeric. Any field missed in that reverse mapping is silently
dropped, and the listing keeps its price while losing its photographs, or keeps
its address while losing the link the delisting check needs to open. So the
first test here is the round trip, field by field.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from app import ingest
from app.models import BatchType, ImportBatch, PropertyForSale


def _batch(db, *, active=True, filename="week1.csv"):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename=filename, is_active=active, status="published")
    db.add(b)
    db.commit()
    return b


# Everything a customer can see or the pipeline can use, filled with values that
# are distinguishable from each other so a mix-up shows as a wrong value rather
# than a passing test.
FULL = dict(
    address="12 Example Road", name="The Example", suburb="Papakura",
    district="Papakura", region="Auckland", postcode="2110",
    latitude=-37.06, longitude=174.94,
    url="https://oneroof.co.nz/listing/12-example", slug_id="12-example-road",
    property_type="House", type_of_title="Freehold", zoning="Mixed Housing Urban",
    land_slope_contour="Easy/moderate fall",
    beds=4, baths=2, cars=1, floor_area_m2=185.0, land_area_m2=812.0,
    asking_price=1_250_000.0, listing_date="2026-07-14", days_on_market=31.0,
    sale_status="For sale", last_updated="2026-08-30",
    cv_numeric=1_150_000.0, land_value_numeric=780_000.0,
    improvement_value_numeric=370_000.0,
    third_party_valuation=1_210_000.0, third_party_valuation_high=1_330_000.0,
    third_party_valuation_low=1_090_000.0,
    valuation_last_date="2024-06-30", valuation_rateable_change_pct=12.5,
    valuation_land_change_pct=15.0, valuation_improvement_change_pct=4.0,
    valuation_last_sold_value=880_000.0, valuation_last_sold_date="2019-03-04",
    sold_listing_date="2019-03-04", sold_listing_price_label="$880,000",
    valuation_trend_yearly_json='{"2024":1150000}',
    valuation_trend_monthly_json='{"2026-08":1210000}',
    sale_history_json='[{"date":"2019-03-04"}]',
    cv_history_json='[{"year":2024}]',
    schools_json='[{"name":"Papakura High"}]',
    image_url="https://img.example/1.jpg",
    image_urls="https://img.example/1.jpg\nhttps://img.example/2.jpg",
    image_count=2,
    key_facts="4 bed, 2 bath", key_time_on_market="31 days",
    estate_description="A big description.",
    council_valuation_summary="CV $1.15M",
    property_trend="Rising", description="Long marketing copy.",
    listing_title="Family home on 812m2", listing_published_date="2026-07-14",
    agent1_name="Jane Agent", agent1_phone="021 000 000",
    agent1_email="jane@agency.co.nz", agent1_job_title="Salesperson",
    agent1_company_name="Agency Ltd",
    agent2_name="Sam Second", agent2_phone="021 111 111",
    agent2_email="sam@agency.co.nz", agent2_job_title="Associate",
    agent2_company_name="Agency Ltd", company_name="Agency Ltd",
    building_age="1970s", parking_covered=1, parking_other=2,
    has_swimming_pool=True, is_new_construction=False,
    is_coastal_waterfront=False, storey_count=1,
    other_features="Deck", prior_asking_price=1_320_000.0,
    lots_override=3.0, pv_estimate_mid=1_190_000.0, homes_valuation=1_205_000.0,
)


def _listing(db, batch, **over):
    row = PropertyForSale(import_batch_id=batch.id, is_held=False,
                          **{**FULL, **over})
    db.add(row)
    db.commit()
    return row


# ---- the round trip ---------------------------------------------------------
def test_a_carried_listing_keeps_everything_a_customer_can_see(db_session):
    """THE ONE THAT MATTERS. A field missed in the reverse mapping is silently
    dropped: the house keeps its price and loses its photographs."""
    b = _batch(db_session)
    row = _listing(db_session, b)
    scrape = ingest._row_to_scrape(row)
    payload = ingest._common_property_payload(scrape, region="Auckland")

    # Every stored value the customer can see has to survive the round trip.
    checked = 0
    missing = []
    for field, before in FULL.items():
        if field not in payload:
            continue                       # not a passthrough field; see below
        after = payload[field]
        checked += 1
        if isinstance(before, float) and after is not None:
            ok = abs(float(after) - before) < 0.01
        else:
            ok = after == before
        if not ok:
            missing.append(f"{field}: {before!r} -> {after!r}")
    assert checked > 40, f"only {checked} fields were actually compared"
    assert missing == [], "lost on the way through:\n  " + "\n  ".join(missing)


def test_the_link_survives_so_the_delisting_check_can_still_open_it(db_session):
    """A carried listing with no url is a listing the daily check can never
    retire — it would sit on the site for ever."""
    b = _batch(db_session)
    row = _listing(db_session, b)
    scrape = ingest._row_to_scrape(row)
    assert scrape["url"] == FULL["url"]
    assert scrape["slug_id"] == FULL["slug_id"]


def test_the_photographs_survive(db_session):
    b = _batch(db_session)
    row = _listing(db_session, b)
    payload = ingest._common_property_payload(ingest._row_to_scrape(row),
                                              region="Auckland")
    assert payload["image_url"] == FULL["image_url"]
    assert payload["image_count"] == FULL["image_count"]


def test_an_operators_hand_set_lot_count_survives(db_session):
    """A correction that does not survive the step after it is not a
    correction."""
    b = _batch(db_session)
    row = _listing(db_session, b)
    assert ingest._row_to_scrape(row)["lots_override"] == 3.0


# ---- what is carried, and what is not ---------------------------------------
def test_a_listing_the_new_file_did_not_mention_is_carried(db_session):
    b = _batch(db_session)
    _listing(db_session, b, slug_id="still-for-sale")
    incoming = pd.DataFrame([{"slug_id": "a-brand-new-listing"}])
    carried = ingest.carry_forward(db_session, "Auckland", incoming)
    assert len(carried) == 1
    assert carried.iloc[0]["slug_id"] == "still-for-sale"


def test_a_listing_the_new_file_DOES_mention_is_not_carried(db_session):
    """The new file is fresher. Carrying both would put the house on the site
    twice, at two prices."""
    b = _batch(db_session)
    _listing(db_session, b, slug_id="mentioned-again")
    incoming = pd.DataFrame([{"slug_id": "mentioned-again"}])
    assert len(ingest.carry_forward(db_session, "Auckland", incoming)) == 0


def test_a_listing_whose_link_is_dead_is_not_carried(db_session):
    """THE WHOLE POINT of the daily check: this is the one signal that means a
    listing is actually over, and it is the only thing that retires one."""
    b = _batch(db_session)
    _listing(db_session, b, slug_id="gone",
             link_dead_at=datetime.now(timezone.utc))
    _listing(db_session, b, slug_id="still-up")
    carried = ingest.carry_forward(db_session, "Auckland",
                                   pd.DataFrame([{"slug_id": "new"}]))
    assert list(carried["slug_id"]) == ["still-up"]


def test_the_first_ever_upload_carries_nothing(db_session):
    """No previous batch, nothing to carry, and no crash reaching for one."""
    carried = ingest.carry_forward(db_session, "Auckland",
                                   pd.DataFrame([{"slug_id": "first"}]))
    assert len(carried) == 0


def test_only_the_active_batch_is_carried(db_session):
    """An archived batch is history. Carrying every batch ever uploaded would
    resurrect listings that came off the market months ago."""
    old = _batch(db_session, active=False, filename="ancient.csv")
    live = _batch(db_session, active=True, filename="week9.csv")
    _listing(db_session, old, slug_id="from-the-archive")
    _listing(db_session, live, slug_id="from-the-live-batch")
    carried = ingest.carry_forward(db_session, "Auckland",
                                   pd.DataFrame([{"slug_id": "new"}]))
    assert list(carried["slug_id"]) == ["from-the-live-batch"]


def test_a_file_with_no_slug_column_carries_everything(db_session):
    """A file we cannot match against is not evidence that anything has gone.
    Carrying everything is the safe direction: the worst case is a duplicate an
    operator can see, against a book silently emptied."""
    b = _batch(db_session)
    _listing(db_session, b, slug_id="keep-me")
    carried = ingest.carry_forward(db_session, "Auckland",
                                   pd.DataFrame([{"address": "1 Somewhere"}]))
    assert len(carried) == 1


def test_a_short_file_no_longer_empties_the_book(db_session):
    """THE REPORTED FAULT. Thirty listings uploaded over three thousand used to
    leave thirty."""
    b = _batch(db_session)
    for i in range(300):
        _listing(db_session, b, slug_id=f"existing-{i}",
                 url=f"https://oneroof.co.nz/listing/{i}")
    incoming = pd.DataFrame([{"slug_id": f"new-{i}"} for i in range(3)])
    carried = ingest.carry_forward(db_session, "Auckland", incoming)
    assert len(carried) == 300, "the book was replaced instead of added to"
