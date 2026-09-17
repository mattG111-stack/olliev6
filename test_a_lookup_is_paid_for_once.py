"""Re-uploading a file must not re-buy every council record.

    "its alot of work to reload all the data everytime and redo the corlgic"

It was, and needlessly. A CoreLogic lookup costs money and a second of
somebody's afternoon, and re-uploading a listings file threw every one away:
the new rows arrive blank, so the enrich stage asks the same question about the
same house it asked last week. On a weekly file that is the entire enrichment
bill, every week, for an answer that has not changed. A house does not grow a
bedroom.

Two things have to travel for that to stop, and carrying one without the other
fixes nothing:

  THE ANSWERS — floor, land, beds, baths, CV, zoning — into the FRAME, before
  the pricing run, because zoning is what decides whether a site is assessed
  for subdivision at all.

  THE STAMP — pv_checked_at — onto the ROW, because that is the field the
  enrich stage skips on. Carry the answers alone and the question gets asked
  all over again.

And the direction matters: only blanks are filled. The file is newer than the
lookup, so a re-scrape that corrects a floor area must never be overwritten by
last week's answer.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app import ingest
from app.ingest import ingest_for_sale, ingest_sold
from app.models import BatchType, ImportBatch, PropertyForSale

MHU = "Residential - Mixed Housing Urban Zone"


def _sold():
    return pd.DataFrame([{
        "address": f"{i} Sold Street", "suburb": "Papakura", "district": "Papakura",
        "region": "Auckland", "property_type": "House", "key_bedrooms": 3,
        "key_bathrooms": 1, "key_floor_area": f"{120 + i} sqm",
        "key_land_area": f"{700 + i * 10} sqm", "cv_numeric": 900_000,
        "price_numeric": 930_000 + i * 10_000, "sale_price": 930_000 + i * 10_000,
        "land_value_numeric": 600_000, "improvement_value_numeric": 300_000,
        "type_of_title": "Freehold", "sold_date": "2026-06-01",
    } for i in range(12)])


def _row(**over):
    row = dict(address="12 Repeat Road", suburb="Papakura", district="Papakura",
               region="Auckland", property_type="House", slug_id="12-repeat-road",
               url="https://oneroof.co.nz/12-repeat-road",
               price_display="$950,000", price_numeric=950_000,
               sale_method="fixed price", cv_numeric=900_000,
               land_value_numeric=600_000, improvement_value_numeric=300_000,
               key_bedrooms=3, key_bathrooms=1, key_carspaces=1,
               key_floor_area=140, key_land_area=800, type_of_title="Freehold")
    row.update(over)
    return row


def _upload(db, rows, filename):
    ingest_for_sale(db, pd.DataFrame(rows), _sold(), filename,
                    region="Auckland", publish=True)
    b = (db.query(ImportBatch)
         .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                 ImportBatch.is_active.is_(True))
         .order_by(ImportBatch.id.desc()).first())
    return {p.slug_id: p for p in db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == b.id).all()}


def _looked_up(db, slug, **answers):
    """Pretend CoreLogic answered for this listing, the way the enrich stage
    records it."""
    p = (db.query(PropertyForSale)
         .filter(PropertyForSale.slug_id == slug)
         .order_by(PropertyForSale.id.desc()).first())
    for k, v in answers.items():
        setattr(p, k, v)
    p.pv_checked_at = datetime.now(timezone.utc)
    db.commit()
    return p


def test_the_second_upload_does_not_need_the_lookup_again(db_session):
    """THE ONE THAT MATTERS. The stamp is what the enrich stage reads."""
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=True)
    _upload(db_session, [_row(zoning=None)], "week1.csv")
    _looked_up(db_session, "12-repeat-road", zoning=MHU, pv_cv=915_000.0,
               pv_estimate_mid=1_020_000.0)

    again = _upload(db_session, [_row(zoning=None)], "week2.csv")["12-repeat-road"]
    assert again.pv_checked_at is not None, "it would be looked up all over again"
    assert again.pv_cv == 915_000.0
    assert again.pv_estimate_mid == 1_020_000.0


def test_the_answers_reach_the_row_not_just_the_record(db_session):
    """Zoning above all: it decides whether the site is assessed for
    subdivision, and it has to be there for the PRICING run, not after it."""
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=True)
    _upload(db_session, [_row(zoning=None)], "week1.csv")
    _looked_up(db_session, "12-repeat-road", zoning=MHU)

    again = _upload(db_session, [_row(zoning=None)], "week2.csv")["12-repeat-road"]
    assert again.zoning == MHU


def test_a_fresh_answer_in_the_file_always_wins(db_session):
    """The file is newer than the lookup. A re-scrape that corrects a floor
    area must not be overwritten by last week's answer."""
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=True)
    _upload(db_session, [_row()], "week1.csv")
    _looked_up(db_session, "12-repeat-road", floor_area_m2=140.0, zoning=MHU)

    again = _upload(db_session, [_row(key_floor_area=185)], "week2.csv")
    assert again["12-repeat-road"].floor_area_m2 == 185.0, "the old answer won"


def test_a_house_never_looked_up_is_untouched(db_session):
    ingest_sold(db_session, _sold(), "sold.csv", region="Auckland", publish=True)
    _upload(db_session, [_row()], "week1.csv")
    new = _upload(db_session, [_row(), _row(slug_id="99-new-road",
                                            address="99 New Road")], "week2.csv")
    assert new["99-new-road"].pv_checked_at is None


def test_the_first_ever_upload_carries_nothing(db_session):
    """No prior batch, nothing to reuse, and no crash reaching for one."""
    out = ingest.carry_forward_enrichment(
        db_session, "Auckland", pd.DataFrame([{"slug_id": "first"}]))
    assert out == {"matched": 0, "cells": 0}


def test_a_file_with_no_slug_column_is_left_alone(db_session):
    out = ingest.carry_forward_enrichment(
        db_session, "Auckland", pd.DataFrame([{"address": "1 Somewhere"}]))
    assert out["matched"] == 0
