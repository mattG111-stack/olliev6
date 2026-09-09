"""A lot count set during the review has to still be there after the publish.

    "i want to be able to edit them before they go live"

The scenario calculator on the property page answers "what if" and saves
nothing. That is the right shape for working a number out and no use at all for
fixing one: you can set a site to six lots, watch the profit change, close the
tab, and nothing happened. A review that changes nothing is not a review.

So the count is STORED on the listing, and the two things that would quietly
undo it are the two things tested hardest here:

    the re-price     runs over the whole batch and recomputes every figure. If
                     it read the lot count from the model it would replace the
                     operator's number on the very next step of the flow — and
                     "Re-run pricing" is a step people press between reviewing
                     and going live.

    the publish      moves the batch from staged to live. The row goes with it,
                     override and all; nothing recomputes.

And one hazard that is not about intent at all. The override now travels
through pandas, and a null in a float column arrives as NaN — which is TRUTHY.
`if lots_override:` therefore fires on every row that has NO override, and
int(NaN) raises, which would take down the pricing run for the whole batch on
the first row it touched.
"""
from __future__ import annotations

import math

import pytest

from app.models import BatchType, ImportBatch, PropertyForSale, PropertySold
from app.pricing.subdivision import compute

ZONE = "Residential - Mixed Housing Suburban Zone"


def _site(**kw):
    base = dict(zone=ZONE, land_area=1400.0, buy_price=1_100_000.0,
                section_rate=1800.0, property_type="House", title_type="Freehold",
                land_value=760_000.0, cv=1_150_000.0)
    base.update(kw)
    return compute(**base)


# ---- the NaN hazard ---------------------------------------------------------
def test_no_override_written_as_nan_is_not_an_override(db_session=None):
    """THE CRASH. A null in a pandas float column is NaN, and NaN is truthy. A
    plain truth test treats "no override" as an override on every row that has
    none, and then int(NaN) raises — taking down the pricing run for the whole
    batch on the first row it reached."""
    model = _site().sections
    assert _site(lots_override=float("nan")).sections == model, \
        "NaN was treated as a hand-set lot count"


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), "", "abc", None])
def test_nonsense_falls_back_to_the_model(bad):
    """Anything that is not a number of lots leaves the model's own reading
    alone, rather than producing a site nobody chose."""
    assert _site(lots_override=bad).sections == _site().sections


@pytest.mark.parametrize("bad", [0, -3, 0.4])
def test_a_count_below_one_lot_is_not_a_subdivision(bad):
    """A typed 0 must land on the ordinary "not subdividable" answer, not invent
    a site."""
    assert _site(lots_override=bad).sections is None


def test_an_override_is_capped_at_what_is_practical():
    """Local knowledge, not a licence to put forty houses on a quarter acre."""
    from app.pricing import assumptions as A

    assert _site(lots_override=9999).sections == int(A.MAX_PRACTICAL_LOTS_TOTAL)


def test_the_override_actually_moves_the_lot_count():
    assert _site().sections == 3
    assert _site(lots_override=6).sections == 6
    assert _site(lots_override=6).max_addl_lots == 5


# ---- it survives the steps that come after it -------------------------------
@pytest.fixture()
def staged(db_session):
    """A staged batch with one subdividable listing, and sales to price against."""
    sold = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                       filename="sold.xlsx", status="published", is_active=True)
    db_session.add(sold)
    db_session.flush()
    for i in range(40):
        db_session.add(PropertySold(
            import_batch_id=sold.id, address=f"{i} Comp Road", suburb="Papakura",
            sale_price=950_000.0, floor_area_m2=150.0, land_area_m2=600.0,
            beds=3, baths=2, property_type="House", type_of_title="Freehold"))
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="week.csv", status="staged", is_active=False)
    db_session.add(b)
    db_session.flush()
    p = PropertyForSale(
        import_batch_id=b.id, address="7 Subdivide Road", suburb="Papakura",
        property_type="House", type_of_title="Freehold", asking_price=1_100_000.0,
        cv_numeric=1_150_000.0, land_value_numeric=760_000.0,
        improvement_value_numeric=390_000.0, floor_area_m2=140.0,
        land_area_m2=1400.0, beds=3, baths=1, zoning=ZONE, is_held=False)
    db_session.add(p)
    db_session.commit()
    return b, p


def test_setting_it_stores_it_and_reprices_the_row(client, staged):
    _b, p = staged
    r = client.post(f"/api/admin/listings/{p.id}/lots", json={"lots": 6})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lots_override"] == 6
    assert body["sections"] == 6, "the row was not re-priced with the new count"


def test_a_full_reprice_does_not_undo_it(client, staged, db_session):
    """THE ONE THAT MATTERS. "Re-run pricing" is a step people press between
    reviewing and going live. If it read the lot count from the model, the
    correction would be gone by the time anyone published."""
    from app.reprice import reprice_batch

    b, p = staged
    # Read the ids out BEFORE the re-price. reprice_batch calls expunge_all()
    # per chunk to keep memory flat, which detaches these objects — touching
    # p.id afterwards raises DetachedInstanceError, and that looks exactly like
    # a product bug when it is only the test holding a stale handle.
    batch_id, listing_id = b.id, p.id
    client.post(f"/api/admin/listings/{listing_id}/lots", json={"lots": 6})

    reprice_batch(db_session, batch_id, region="Auckland", commit=True)

    db_session.expire_all()
    after = db_session.get(PropertyForSale, listing_id)
    assert after.lots_override == 6, "the re-price wiped the operator's number"
    assert after.sections == 6, "the re-price recomputed the lot count from the model"


def test_clearing_it_hands_the_site_back_to_the_model(client, staged, db_session):
    """Null is not the same as a number, including a number equal to the
    model's — clearing has to restore the model's own reading rather than
    freeze whatever it said today."""
    _b, p = staged
    client.post(f"/api/admin/listings/{p.id}/lots", json={"lots": 6})
    r = client.post(f"/api/admin/listings/{p.id}/lots", json={"lots": None})

    assert r.status_code == 200, r.text
    assert r.json()["lots_override"] is None
    db_session.expire_all()
    assert db_session.get(PropertyForSale, p.id).lots_override is None


def test_the_publish_carries_it_across(client, staged, db_session):
    """Publishing moves the batch; the row goes with it, override and all."""
    from app.release import publish_release

    b, p = staged
    client.post(f"/api/admin/listings/{p.id}/lots", json={"lots": 6})
    publish_release(db_session, region="Auckland")

    db_session.expire_all()
    after = db_session.get(PropertyForSale, p.id)
    assert after.lots_override == 6
    assert after.sections == 6


def test_a_count_below_one_is_refused_in_words(client, staged):
    """An operator who types 0 should be told what to do instead, not handed a
    site with no lots on it."""
    _b, p = staged
    r = client.post(f"/api/admin/listings/{p.id}/lots", json={"lots": 0})
    assert r.status_code == 422
    assert "at least one lot" in r.json()["detail"]


def test_setting_it_survives_having_no_sales_to_price_against(client, db_session):
    """The SAVE is the point and it has already happened by the time the
    re-price runs. Losing the operator's number because the figures could not be
    refreshed yet would be the wrong way round."""
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="week.csv", status="staged", is_active=False)
    db_session.add(b)
    db_session.flush()
    p = PropertyForSale(import_batch_id=b.id, address="1 Lonely Road",
                        suburb="Papakura", property_type="House",
                        type_of_title="Freehold", land_area_m2=1400.0,
                        zoning=ZONE, is_held=False)
    db_session.add(p)
    db_session.commit()                       # no sold batch at all

    r = client.post(f"/api/admin/listings/{p.id}/lots", json={"lots": 5})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(PropertyForSale, p.id).lots_override == 5


def test_an_unknown_listing_is_a_404(client):
    assert client.post("/api/admin/listings/999999/lots",
                       json={"lots": 3}).status_code == 404


@pytest.fixture()
def client(db_session):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_db
    from app.models import User, UserRole, UserStatus
    from app.security import current_user, require_active, require_admin

    admin = User(email="lots-admin@test.local", password_hash="x",
                 role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value)
    db_session.add(admin)
    db_session.commit()
    main.app.dependency_overrides = {
        get_db: lambda: db_session,
        current_user: lambda: admin,
        require_active: lambda: admin,
        require_admin: lambda: admin,
    }
    try:
        yield TestClient(main.app, raise_server_exceptions=False)
    finally:
        main.app.dependency_overrides = {}
