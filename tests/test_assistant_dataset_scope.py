"""Execute category queries against overlapping snapshot/history fixtures."""
import json

from sqlalchemy import text

from assistant import sql
from ingest import sold_batch_ids
from models import ImportBatch, PropertyRent, PropertySold


def test_categories_match_rental_snapshots_and_valuation_history(db_session, monkeypatch):
    db = db_session

    def batch(kind, active=False, status="published", region="Auckland"):
        b = ImportBatch(batch_type=kind, region=region, filename="fixture.csv",
                        is_active=active, status=status)
        db.add(b)
        db.flush()
        return b.id

    rent_old = batch("rent")
    rent_live = batch("rent", True)
    rent_other = batch("rent", True, region="Wellington")
    sold_old = batch("sold")
    sold_live = batch("sold", True)
    sold_staged = batch("sold", status="staged")
    sold_preview = batch("sold", status="preview")
    sold_rejected = batch("sold", status="failed")
    for bid in (rent_old, rent_old, rent_live, rent_other):
        db.add(PropertyRent(import_batch_id=bid, address="Rental fixture", suburb="Rental suburb"))
    for bid in (sold_old, sold_live, sold_staged, sold_preview, sold_rejected):
        db.add(PropertySold(import_batch_id=bid, address="Sale fixture", suburb="Sale suburb"))
    db.commit()

    def execute(query):
        return json.dumps([dict(r) for r in db.execute(text(query)).mappings()])

    monkeypatch.setattr(sql, "run", execute)
    assert json.loads(sql.distinct_values("properties_rent", "suburb")) == [
        {"value": "Rental suburb", "n": 2}]
    eligible = sold_batch_ids(db, "Auckland")
    assert set(eligible) == {sold_old, sold_live, sold_staged, sold_preview}
    assert json.loads(sql.distinct_values("properties_sold", "suburb")) == [
        {"value": "Sale suburb", "n": len(eligible)}]


def test_no_active_rental_snapshot_returns_no_categories(db_session, monkeypatch):
    db = db_session
    b = ImportBatch(batch_type="rent", filename="old.csv", is_active=False)
    db.add(b)
    db.flush()
    db.add(PropertyRent(import_batch_id=b.id, address="Old rental", suburb="Old suburb"))
    db.commit()
    monkeypatch.setattr(sql, "run", lambda query: list(db.execute(text(query))))
    assert sql.distinct_values("properties_rent", "suburb") == []
