"""A property found by a portal sweep queues with everything else.

Approving one used to put it straight into the LIVE batch. That is the one
route into the feed that skipped the staged review — no photo check, no Hide,
no preview — and it is the route carrying the LEAST verified data, because a
portal row is scraped off someone else's page with no council record behind it.
Everything that arrives in the weekly file gets looked at before a customer
sees it; the rows we were least sure about did not.

It joins the staged batch now when there is one, and reaches customers when
that batch is published, like everything else. With nothing staged it still
goes live immediately — that is the ordinary mid-week case, a property found
between loads with no review pending to join.

The duplicate check moved with it. "Do we already have this property" has to
mean live OR staged: once approvals land in staged, an address can be in the
staged file and not yet live, and a check that only looked at live would offer
it again and put a duplicate in front of the reviewer.
"""
from __future__ import annotations

import pytest

from app.models import (BatchType, ImportBatch, PortalListing, PropertyForSale)


def _batch(db, *, status, active):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename=f"{status}.csv", status=status, is_active=active)
    db.add(b)
    db.flush()
    return b


def _found(db, address="12 Found By Portal Road", suburb="Glenfield"):
    from app.addresses import address_key

    row = PortalListing(
        source="oneroof", kind="for_sale", status="pending",
        address=address, suburb=suburb, address_key=address_key(address, suburb),
        property_type="House", price_numeric=900_000.0, cv_numeric=950_000.0,
        floor_area_m2=150.0, land_area_m2=600.0, beds=3, baths=2)
    db.add(row)
    db.commit()
    return row


def test_an_approval_joins_the_batch_being_reviewed(db_session):
    """THE FIX. A staged batch exists, so the find queues behind the same
    review as the weekly file rather than appearing in front of customers."""
    from app.portals.listings import approve

    live = _batch(db_session, status="published", active=True)
    staged = _batch(db_session, status="staged", active=False)
    row = _found(db_session)

    added, why = approve(db_session, row.id)
    assert added, why

    p = db_session.get(PropertyForSale, row.property_id)
    assert p.import_batch_id == staged.id, (
        "an unreviewed portal find went straight into the live batch")
    assert p.import_batch_id != live.id


def test_a_preview_batch_counts_as_being_reviewed(db_session):
    """Preview is the stage where the reviewing actually happens, so a find
    approved during it belongs there and not in front of customers."""
    from app.portals.listings import approve

    _batch(db_session, status="published", active=True)
    preview = _batch(db_session, status="preview", active=False)
    row = _found(db_session)

    added, why = approve(db_session, row.id)
    assert added, why
    assert db_session.get(PropertyForSale, row.property_id).import_batch_id == preview.id


def test_with_nothing_staged_it_still_goes_live(db_session):
    """The counterweight, and the reason this feature exists: a property found
    between weekly loads, with no review pending to join. Sending it nowhere
    would be worse than sending it live."""
    from app.portals.listings import approve

    live = _batch(db_session, status="published", active=True)
    row = _found(db_session)

    added, why = approve(db_session, row.id)
    assert added, why
    assert db_session.get(PropertyForSale, row.property_id).import_batch_id == live.id


def test_with_no_batch_at_all_it_says_so_rather_than_failing_quietly(db_session):
    from app.portals.listings import approve

    row = _found(db_session)
    added, why = approve(db_session, row.id)
    assert not added
    assert "no batch" in why


def test_an_address_already_in_the_staged_file_is_not_offered_again(db_session):
    """Once approvals land in staged, an address can be in the staged file and
    not yet live. A duplicate check that only looked at live would offer it a
    second time and put two of the same property in front of the reviewer."""
    from app.portals.listings import _live_keys
    from app.addresses import address_key

    staged = _batch(db_session, status="staged", active=False)
    db_session.add(PropertyForSale(
        import_batch_id=staged.id, address="12 Found By Portal Road",
        suburb="Glenfield", property_type="House", asking_price=900_000.0,
        floor_area_m2=150.0, is_held=False))
    db_session.commit()

    assert address_key("12 Found By Portal Road", "Glenfield") in _live_keys(db_session)


def test_an_address_already_live_is_still_not_offered_again(db_session):
    """The half that already worked, asserted so widening the check cannot
    quietly drop it."""
    from app.portals.listings import _live_keys
    from app.addresses import address_key

    live = _batch(db_session, status="published", active=True)
    db_session.add(PropertyForSale(
        import_batch_id=live.id, address="9 Already Live Street", suburb="Remuera",
        property_type="House", asking_price=800_000.0, floor_area_m2=140.0,
        is_held=False))
    db_session.commit()

    assert address_key("9 Already Live Street", "Remuera") in _live_keys(db_session)


def test_a_find_that_the_weekly_file_already_carries_is_superseded(db_session):
    """The good outcome: the file arrived with the property in it between the
    sweep and the decision, and the file's row has the council record the
    scraped one does not."""
    from app.portals.listings import approve

    staged = _batch(db_session, status="staged", active=False)
    db_session.add(PropertyForSale(
        import_batch_id=staged.id, address="12 Found By Portal Road",
        suburb="Glenfield", property_type="House", asking_price=900_000.0,
        floor_area_m2=150.0, is_held=False))
    db_session.commit()
    row = _found(db_session)

    added, why = approve(db_session, row.id)
    assert not added
    assert "already has this property" in why
    db_session.refresh(row)
    assert row.status == "superseded"


def test_a_sale_still_goes_to_the_sold_pool(db_session):
    """Sold rows are not listings and are not reviewed: a sale is not valued,
    it is what everything else is valued against. This change must not have
    dragged them into the staged batch."""
    from app.models import PropertySold
    from app.portals.listings import approve

    _batch(db_session, status="staged", active=False)
    sale = PortalListing(
        source="oneroof", kind="sold", status="pending",
        address="3 Sold Street", suburb="Glenfield", address_key="3 sold street|glenfield",
        property_type="House", sale_price=1_000_000.0, floor_area_m2=150.0,
        beds=3, baths=2)
    db_session.add(sale)
    db_session.commit()

    added, why = approve(db_session, sale.id)
    assert added, why
    assert db_session.get(PropertySold, sale.property_id) is not None
