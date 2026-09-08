"""Knowing a customer by what they are looking at.

Three sources, in order of how directly they state intent:

  * a wish list — a suburb, a price band and a bed count they typed in
    themselves, which is as close to a brief as this product gets
  * the questions they ask — the suburbs in them, and the last few verbatim,
    because "is Glenfield still soft?" says something a filter cannot
  * where their time goes — which parts of the product they actually open

And one source deliberately NOT used: which listings they opened. page_views
records the route and never the id, so that a usage table cannot become a record
of who looked at whose house. These tests hold that line too.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.models import AssistantLog, PageView, User, WishList
from app.routers.admin_metrics import user_interests
from app.security import hash_password

NOW = datetime.now(timezone.utc)


def _user(db, email, name="A Buyer", role="user"):
    u = User(email=email, password_hash=hash_password("x"), full_name=name,
             role=role, status="approved")
    db.add(u); db.commit(); db.refresh(u)
    return u


def _rows(db, days=30):
    return {r.email: r for r in user_interests(days=days, _=None, db=db).rows}


def test_a_wish_list_is_read_as_a_brief(db_session):
    db = db_session
    u = _user(db, "amy@apexdemo.co.nz", "Amy Buyer")
    db.add(WishList(user_id=u.id, name="Remuera family home", suburb="Remuera",
                    min_price=1_200_000, max_price=2_000_000, min_beds=4,
                    underpriced_only=True, subdividable_only=False))
    db.commit()

    r = _rows(db)["amy@apexdemo.co.nz"]
    assert "Remuera" in r.suburbs
    assert r.price_low == 1_200_000 and r.price_high == 2_000_000
    assert r.min_beds == 4
    assert r.wants == ["underpriced"]


def test_two_wish_lists_widen_the_band_rather_than_replacing_it(db_session):
    """Someone shopping two briefs is shopping the union of them."""
    db = db_session
    u = _user(db, "bob@apexdemo.co.nz")
    db.add(WishList(user_id=u.id, name="one", suburb="Remuera",
                    min_price=1_500_000, max_price=2_000_000, min_beds=4))
    db.add(WishList(user_id=u.id, name="two", suburb="Glenfield",
                    min_price=900_000, max_price=1_300_000, min_beds=3,
                    subdividable_only=True))
    db.commit()

    r = _rows(db)["bob@apexdemo.co.nz"]
    assert set(r.suburbs) == {"Glenfield", "Remuera"}
    assert r.price_low == 900_000 and r.price_high == 2_000_000
    assert r.min_beds == 3, "the lower bed count is still in scope"
    assert r.wants == ["subdividable"]


def test_the_suburbs_they_ask_about_count_too(db_session):
    """A question names a place no filter has been saved for."""
    db = db_session
    u = _user(db, "cara@apexdemo.co.nz")
    # Suburb names are matched against what is actually loaded, so seed one.
    db.execute(WishList.__table__.insert().values(
        user_id=u.id, name="w", suburb="Remuera", underpriced_only=False,
        subdividable_only=False))
    db.add(AssistantLog(user_id=u.id, question="How is Browns Bay selling?",
                        answer="Like this.", ok=True))
    db.commit()

    # Browns Bay has to exist in the for-sale data to be recognised.
    db.execute(_for_sale_insert(db, "Browns Bay"))
    db.commit()

    r = _rows(db)["cara@apexdemo.co.nz"]
    assert "Browns Bay" in r.suburbs, r.suburbs
    assert r.last_questions == ["How is Browns Bay selling?"]
    assert r.questions == 1


def _for_sale_insert(db, suburb):
    """A minimal for-sale row, so the suburb is one the matcher knows."""
    from app.models import ImportBatch, PropertyForSale
    batch = ImportBatch(batch_type="for_sale", region="Auckland", filename="x.csv",
                        rows_total=1, is_active=True, status="published")
    db.add(batch); db.flush()
    return PropertyForSale.__table__.insert().values(
        import_batch_id=batch.id, region="Auckland", suburb=suburb,
        address=f"1 Test St, {suburb}", is_held=False)


def test_time_on_the_platform_ranks_the_list(db_session):
    db = db_session
    quiet = _user(db, "quiet@apexdemo.co.nz")
    busy = _user(db, "busy@apexdemo.co.nz")
    db.add(PageView(user_id=quiet.id, path="/today", seconds=60))
    for _ in range(5):
        db.add(PageView(user_id=busy.id, path="/properties", seconds=300))
    db.add(PageView(user_id=busy.id, path="/trends", seconds=120))
    db.commit()

    rows = user_interests(days=30, _=None, db=db).rows
    assert [r.email for r in rows][0] == "busy@apexdemo.co.nz"
    top = {r.email: r for r in rows}["busy@apexdemo.co.nz"]
    assert top.minutes == 27.0
    assert top.top_pages[0] == "/properties"


def test_someone_who_has_done_nothing_is_left_off(db_session):
    db = db_session
    _user(db, "ghost@apexdemo.co.nz")
    assert "ghost@apexdemo.co.nz" not in _rows(db)


def test_a_promoter_is_not_profiled_as_a_customer(db_session):
    """They are selling the product, not shopping in it."""
    db = db_session
    p = _user(db, "promo@apexdemo.co.nz", "Promoter", role="promoter")
    db.add(PageView(user_id=p.id, path="/promoter", seconds=900))
    db.commit()
    assert "promo@apexdemo.co.nz" not in _rows(db)


def test_which_listings_they_opened_is_still_not_recorded(db_session):
    """The line page_views draws, held on purpose.

    If this ever fails, someone has started storing ids in the usage table and
    the interests panel would begin reporting who looked at whose house.
    """
    db = db_session
    u = _user(db, "dee@apexdemo.co.nz")
    db.add(PageView(user_id=u.id, path="/property", seconds=200))
    db.commit()
    r = _rows(db)["dee@apexdemo.co.nz"]
    assert r.top_pages == ["/property"]
    assert all("/property/" not in p for p in r.top_pages), (
        "a listing id reached the usage table"
    )
    assert not any(ch.isdigit() for p in r.top_pages for ch in p)
