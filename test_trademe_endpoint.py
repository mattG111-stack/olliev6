"""The Trade Me panel, end to end, through the real endpoint.

The unit tests prove the matching and the filling. They do not prove the thing
an admin actually touches: a file chosen in a browser, posted as multipart, read
by the endpoint, answered with a result the panel can render. Every one of those
steps has its own way of failing, and none of them is exercised by calling
trademe.fill() directly.

The real export is used when it is on this machine — 59,445 rows, which is also
the only honest way to find out whether the endpoint returns before a browser
gives up on it.
"""
from __future__ import annotations

import io
import os
import time

import pytest

from test_endpoints_smoke import _client


@pytest.fixture()
def client(db_session):
    """The real app with the real routes, pointed at the test database.

    Reuses the smoke suite's harness deliberately: same seeded world, same auth
    overrides, so this file tests the endpoint rather than a second fixture.
    """
    yield from _client(db_session)

REAL_EXPORT = ("/root/.claude/uploads/79b70b38-d017-5982-b698-8174439cded8/"
               "59c22150-trademe_export_a_sales_auckland_since20240815_1.csv")

CSV = """address,suburb,city,latitude,longitude,property_type,property_type_confidence,ownership_type,sale_date,sale_price,sale_display_price,floor_area_m2,land_area_m2,est_value,est_value_low,est_value_high,est_value_date,capital_value,land_value,improvement_value,cv_revision_date,cover_image_url
"0 Smoke St, Remuera, Auckland City",Remuera,Auckland City,-36.88,174.79,House,high,Freehold,6/13/2026,1250000,"$1,250,000",210,640,$1.24M,$1.17M,$1.31M,6-Aug-26,1200000,800000,400000,1-May-24,https://example.invalid/a.jpg
"99 Nowhere Road, Elsewhere, Somewhere",Elsewhere,Somewhere,-36.90,174.70,House,high,Freehold,4/30/2026,900000,"$900,000",150,500,$890K,$840K,$940K,6-Aug-26,880000,600000,280000,1-May-24,
"""


def _post(client, body: bytes, name="trademe.csv", **params):
    return client.post(
        "/api/admin/trademe-fill",
        params=params,
        files={"file": (name, io.BytesIO(body), "text/csv")},
    )


def test_a_file_chosen_in_the_browser_comes_back_with_a_result(client):
    c, _admin = client
    r = _post(c, CSV.encode(), dry_run=True)
    assert r.status_code == 200, r.text
    body = r.json()
    # Everything the panel renders has to be in the response.
    for field in ("rows_seen", "matched", "unmatched", "valuations", "filled",
                  "conflicts", "note", "dry_run"):
        assert field in body, f"the panel reads {field} and it is not there"
    assert body["rows_seen"] == 2
    assert body["dry_run"] is True


def test_it_matches_a_property_we_hold_and_leaves_the_rest(client):
    """The seeded world has "0 Smoke St, Remuera". It does not have Nowhere Road."""
    c, _admin = client
    body = _post(c, CSV.encode(), dry_run=True).json()
    assert body["matched"] >= 1, "an address we hold was not matched"
    assert body["unmatched"] == 1, "a property we do not hold was not left alone"


def test_a_dry_run_writes_nothing_and_then_the_real_run_does(client, db_session):
    from app.models import PropertySold

    c, _admin = client
    p = db_session.query(PropertySold).filter(
        PropertySold.address == "0 Smoke St").first()
    assert p is not None, "the seeded property this test needs is gone"
    p.floor_area_m2 = None
    p.tm_valuation = None
    db_session.commit()

    _post(c, CSV.encode(), dry_run=True)
    db_session.refresh(p)
    assert p.floor_area_m2 is None, "a dry run wrote to the database"

    r = _post(c, CSV.encode(), dry_run=False)
    assert r.status_code == 200, r.text
    db_session.refresh(p)
    assert p.floor_area_m2 == 210.0
    assert p.tm_valuation == 1_240_000


def test_a_file_that_is_not_theirs_is_refused_rather_than_half_read(client):
    c, _admin = client
    r = _post(c, b"who,what\n1,2\n", name="wrong.csv", dry_run=True)
    assert r.status_code == 400, r.text
    assert "address" in r.json()["detail"].lower()


def test_something_that_is_not_a_csv_at_all_is_refused(client):
    c, _admin = client
    r = _post(c, b"\x00\x01\x02 not a file", name="junk.bin", dry_run=True)
    assert r.status_code == 400, r.text


def test_it_needs_an_admin():
    """The override in the client fixture makes everyone an admin, so this
    checks the route is declared with the dependency rather than trusting it."""
    import app.main as main
    from app.security import require_admin

    route = next(r for r in main.app.routes
                 if getattr(r, "path", "") == "/api/admin/trademe-fill")
    deps = [d.call for d in route.dependant.dependencies]
    assert require_admin in deps, "the Trade Me endpoint is not admin-only"


@pytest.mark.skipif(not os.path.exists(REAL_EXPORT),
                    reason="the real Trade Me export is not on this machine")
def test_the_real_export_goes_through_the_endpoint_in_reasonable_time(client):
    """59,445 rows. A browser waiting on this is the actual test.

    Also the only place the month-first dates, the shorthand money and the
    address shapes are all exercised at once against a file nobody wrote for us.
    """
    c, _admin = client
    with open(REAL_EXPORT, "rb") as f:
        raw = f.read()

    start = time.monotonic()
    r = _post(c, raw, name="trademe_auckland.csv", dry_run=True)
    took = time.monotonic() - start

    assert r.status_code == 200, r.text[:400]
    body = r.json()
    assert body["rows_seen"] > 55_000, body["rows_seen"]
    assert took < 120, f"took {took:.0f}s — a browser will have given up"
    # Their file covers all of Auckland; the seeded world is three suburbs.
    assert body["unmatched"] > 50_000
