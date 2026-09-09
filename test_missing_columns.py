"""A column added to a model has to reach the database, or the site goes down.

Four tm_valuation columns were added to the property models. They were also
added to the hand-written list in db_bootstrap — a module the production start
command does not run, which the lifespan in app/main.py already says in a
comment. create_all() builds missing TABLES and will not alter one that exists,
so nothing carried the columns across.

Every ORM query selects every mapped column, so the first request after that
deploy answered:

    ProgrammingError: column properties_for_sale.tm_valuation does not exist

/api/properties, /api/properties/suburb-stats and /api/dashboards/today, all at
once, from a column no feature had used yet. The reports came back marked
blocker.

What is tested here is the general case, not those four columns: the models are
the source of truth for the schema, so a model column the database lacks must be
added on startup whatever it is called and whenever it was added.
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from app.db_bootstrap import ensure_columns
from app.models import BatchType, ImportBatch, PropertyForSale


def _drop_column(db, table: str, column: str) -> None:
    """Put the database back the way production was: the table, minus a column."""
    db.commit()
    with db.bind.begin() as conn:
        conn.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))


def _has(db, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(db.bind).get_columns(table)}


@pytest.fixture()
def listing(db_session):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="live.csv", rows_total=1, is_active=True,
                    status="published")
    db_session.add(b); db_session.flush()
    db_session.add(PropertyForSale(
        import_batch_id=b.id, region="Auckland", suburb="Mount Eden",
        address="1 Column Street", asking_price=1_000_000, floor_area_m2=120,
        property_type="House", is_held=False))
    db_session.commit()
    return b


def test_a_model_column_the_database_lacks_takes_queries_down(db_session, listing):
    """The outage itself, reproduced. This is what production was doing."""
    _drop_column(db_session, "properties_for_sale", "tm_valuation")
    assert not _has(db_session, "properties_for_sale", "tm_valuation")

    db_session.expire_all()
    with pytest.raises(Exception) as err:
        db_session.query(PropertyForSale).first()
    assert "tm_valuation" in str(err.value), str(err.value)


def test_startup_adds_it_back(db_session, listing):
    """And this is what stops it happening again — for any column, not this one."""
    _drop_column(db_session, "properties_for_sale", "tm_valuation")

    added = ensure_columns(db_session.bind)
    assert "properties_for_sale.tm_valuation" in added, added
    assert _has(db_session, "properties_for_sale", "tm_valuation")

    db_session.expire_all()
    row = db_session.query(PropertyForSale).first()
    assert row is not None and row.tm_valuation is None


def test_it_finds_every_missing_column_not_just_the_first(db_session, listing):
    for col in ("tm_valuation", "tm_valuation_low", "tm_valuation_high",
                "tm_valuation_date"):
        _drop_column(db_session, "properties_for_sale", col)

    added = ensure_columns(db_session.bind)
    assert len([a for a in added if a.startswith("properties_for_sale.")]) == 4, added
    db_session.expire_all()
    assert db_session.query(PropertyForSale).first() is not None


def test_it_does_nothing_when_the_schema_is_already_right(db_session, listing):
    """It runs on every boot, so it has to be free when there is nothing to do."""
    assert ensure_columns(db_session.bind) == []


def test_the_column_comes_back_nullable(db_session, listing):
    """A table with rows cannot take a NOT NULL column with no default.

    The models are the authority on nullability and _reconcile_nullability
    settles it separately. Here, a live site beats a constraint.
    """
    _drop_column(db_session, "properties_for_sale", "tm_valuation")
    ensure_columns(db_session.bind)
    col = next(c for c in inspect(db_session.bind).get_columns("properties_for_sale")
               if c["name"] == "tm_valuation")
    assert col["nullable"] is True


def test_the_models_and_the_database_agree_after_it_runs(db_session, listing):
    """The invariant, stated once: nothing the models declare is missing.

    Catches a column added to any model in any table, which is the failure this
    file exists for — the four that took the site down were only the first.
    """
    ensure_columns(db_session.bind)
    insp = inspect(db_session.bind)
    tables = set(insp.get_table_names())
    from app.db import Base

    missing = []
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        missing += [f"{table.name}.{c.name}" for c in table.columns
                    if c.name not in have]
    assert not missing, f"declared by the models, absent from the database: {missing}"
