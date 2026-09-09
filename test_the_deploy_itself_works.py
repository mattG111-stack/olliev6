"""The step nothing tested: what happens to a database that already has rows.

Every test in this suite builds its schema with create_all, so every column
exists from the first line and no row is ever missing one. Production is the
opposite: the table is already there, full of listings, and a new column arrives
by ALTER TABLE with NULL in it for every existing row.

Three of today's faults lived in exactly that gap and none of them could be seen
from a green suite:

  * the Subdividable page empty on deploy, because its filter reads a column
    that is NULL everywhere until something re-prices;
  * the listing endpoint answering 500, because the response model declared that
    column a plain bool and NULL is not one;
  * the invariant between the two subdivision flags broken on old rows, so a
    listing could show a subdivision deal on a site the filter cannot find.

So this test IS the deploy: a table built without the new columns, rows in it,
then the bootstrap, then the questions the site asks afterwards.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.fixture()
def legacy_db(monkeypatch):
    """A properties_for_sale that predates today — no can_subdivide, no
    sale_method — with three rows in it."""
    path = Path(tempfile.mkdtemp()) / "legacy.db"
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as c:
        c.execute(text("""
            CREATE TABLE properties_for_sale (
                id INTEGER PRIMARY KEY,
                import_batch_id INTEGER NOT NULL,
                address VARCHAR(300),
                asking_price FLOAT,
                listing_type VARCHAR(32),
                max_addl_lots FLOAT,
                is_subdividable BOOLEAN
            )"""))
        # A splittable site the old code judged unprofitable, a site it judged
        # worth splitting, and a house that cannot be split at all.
        c.execute(text("INSERT INTO properties_for_sale "
                       "(id, import_batch_id, address, max_addl_lots, is_subdividable)"
                       " VALUES (1, 1, '1 Wide Section Rd', 2.0, 0)"))
        c.execute(text("INSERT INTO properties_for_sale "
                       "(id, import_batch_id, address, max_addl_lots, is_subdividable)"
                       " VALUES (2, 1, '2 Profitable Rd', 3.0, 1)"))
        c.execute(text("INSERT INTO properties_for_sale "
                       "(id, import_batch_id, address, max_addl_lots, is_subdividable)"
                       " VALUES (3, 1, '3 Ordinary Ave', NULL, 0)"))

    from app import db_bootstrap
    monkeypatch.setattr(db_bootstrap, "engine", eng)
    return eng, db_bootstrap


def _cols(eng) -> set[str]:
    return {c["name"] for c in inspect(eng).get_columns("properties_for_sale")}


def test_the_new_columns_arrive(legacy_db):
    eng, boot = legacy_db
    assert "can_subdivide" not in _cols(eng)
    boot.ensure_columns(bind=eng)
    cols = _cols(eng)
    assert "can_subdivide" in cols
    assert "sale_method" in cols


def test_the_page_is_not_empty_the_morning_after(legacy_db):
    """THE ONE THAT MATTERS. Deploy alone would have left every existing
    listing reading NULL, and the subdivision filter reads that column — so the
    Subdividable page would have been blank until the next upload, which is not
    a thing anyone would read as anything but broken.

    It does not need the model re-run. Feasibility was already recorded under
    another name: max_addl_lots is written for every site the pipeline found
    splittable and left NULL for the rest.
    """
    eng, boot = legacy_db
    boot.ensure_columns(bind=eng)
    assert boot._backfill_can_subdivide() == 3

    with eng.begin() as c:
        got = dict(c.execute(text(
            "SELECT address, can_subdivide FROM properties_for_sale")).all())
    assert got["1 Wide Section Rd"] == 1, "a splittable site was left invisible"
    assert got["2 Profitable Rd"] == 1
    assert got["3 Ordinary Ave"] == 0


def test_the_invariant_holds_on_old_rows_too(legacy_db):
    """"Worth subdividing" must never outlive "can be subdivided", or a listing
    shows a subdivision deal on a site the filter cannot find."""
    eng, boot = legacy_db
    boot.ensure_columns(bind=eng)
    boot._backfill_can_subdivide()
    with eng.begin() as c:
        broken = c.execute(text(
            "SELECT COUNT(*) FROM properties_for_sale "
            "WHERE is_subdividable = 1 AND can_subdivide IS NOT 1")).scalar()
    assert broken == 0


def test_running_it_twice_changes_nothing(legacy_db):
    """Every boot runs this. The second one must find nothing left to do, or a
    restart quietly rewrites rows a re-price has since corrected."""
    eng, boot = legacy_db
    boot.ensure_columns(bind=eng)
    assert boot._backfill_can_subdivide() == 3
    assert boot._backfill_can_subdivide() == 0


def test_a_missing_table_does_not_stop_the_boot(monkeypatch):
    """A fresh database has no properties_for_sale yet. The backfill must shrug
    rather than take the deploy down with it."""
    from app import db_bootstrap

    path = Path(tempfile.mkdtemp()) / "empty.db"
    monkeypatch.setattr(db_bootstrap, "engine", create_engine(f"sqlite:///{path}"))
    assert db_bootstrap._backfill_can_subdivide() == 0


# ---- and the response model survives what it will actually be handed ---------
def test_a_listing_with_no_answer_yet_does_not_500_the_endpoint():
    """Between the ALTER TABLE and the backfill — and on any row the backfill
    could not judge — this column is NULL. A plain `bool` on the response model
    turns each of those into a validation error, which is the whole listing
    endpoint answering 500 on the first request after a deploy."""
    from app.routers.properties import ForSaleRow

    fields = ForSaleRow.model_fields
    assert "can_subdivide" in fields
    ok = ForSaleRow.model_construct(can_subdivide=None)
    assert ok.can_subdivide is None

    import pydantic
    try:
        ForSaleRow.__pydantic_validator__.validate_assignment(
            ForSaleRow.model_construct(), "can_subdivide", None)
    except pydantic.ValidationError as exc:            # pragma: no cover
        pytest.fail(f"a NULL can_subdivide is rejected by the response model: {exc}")
