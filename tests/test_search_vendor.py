import json
from models import ImportBatch, PropertyForSale, BatchType
from assistant.tools import search_listings


def _batch(s):
    b = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                    filename="f.csv", rows_total=0, is_active=True, status="published")
    s.add(b); s.flush(); return b


def _row(s, b, addr, desc, asking, cv):
    s.add(PropertyForSale(
        import_batch_id=b.id, address=addr, suburb="Testville", property_type="House",
        beds=3, baths=1, floor_area_m2=110, land_area_m2=400,
        asking_price=asking, cv_numeric=cv, fair_value=cv, is_held=False,
        description=desc))


def test_vendor_filter_and_labels(db_session):
    b = _batch(db_session)
    _row(db_session, b, "1 A St", "Mortgagee sale — must be sold this weekend", 800000, 900000)
    _row(db_session, b, "2 B St", "Character home, monolithic clad, needs a full reclad", 700000, 760000)
    _row(db_session, b, "3 C St", "Beautifully renovated family home", 850000, 870000)
    db_session.commit()

    allr = json.loads(search_listings(suburb="Testville"))
    assert allr["count"] == 3
    # every row carries a vendor_signal field (None when nothing fired)
    assert all("vendor_signal" in x for x in allr["listings"])

    mort = json.loads(search_listings(suburb="Testville", vendor="mortgagee"))
    addrs = {x["address"] for x in mort["listings"]}
    assert addrs == {"1 A St"}
    assert "mortgagee" in (next(iter(mort["listings"]))["vendor_signal"] or "")

    motivated = json.loads(search_listings(suburb="Testville", vendor="motivated"))
    assert "1 A St" in {x["address"] for x in motivated["listings"]}

    no_leaky = json.loads(search_listings(suburb="Testville", exclude_leaky=True))
    assert "2 B St" not in {x["address"] for x in no_leaky["listings"]}
    assert no_leaky["count"] == 2
