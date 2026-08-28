"""How a suburb's sales split by sale method, and what that split rests on.

Two things this guards.

First, every sale must be accounted for. The table used to show only sales with
both a usable council CV and a recognised method code, dropping the rest in
silence — 111 sales presented as 66 auctions and one tender, with the missing 45
reading as some unnamed method.

Second, an unlabelled sale is now COUNTED as private treaty. That is a
deliberate inference, not a measurement: auction, tender and private treaty are
effectively the whole universe here, and the two that get recorded are the two
that are publicly scheduled. The newer export carries no method field at all, so
without this every row it loads would fall out of the table. Because the
resulting figure is one people negotiate against, the count of inferred rows is
reported separately — a split resting mostly on inference is weaker evidence
than one measured from labelled sales, and the percentages alone cannot show it.
"""
from __future__ import annotations

import pytest

from app.models import BatchType, ImportBatch, PropertySold
from app.routers.properties import (
    NEGOTIATION_LABEL,
    _method_label,
    _method_mix_for_suburb,
    _method_recorded,
)

REGION = "Auckland"
SUBURB = "Remuera"


@pytest.fixture()
def sold_batch(db_session):
    b = ImportBatch(batch_type=BatchType.SOLD.value, region=REGION,
                    filename="sold.csv", rows_total=0, is_active=True,
                    status="published")
    db_session.add(b)
    db_session.flush()
    return b


def _add(db, batch, n, method, price, cv, suburb=SUBURB):
    for i in range(n):
        db.add(PropertySold(slug_id=f"{method}-{price}-{i}", address=f"{i} St",
                            suburb=suburb, region=REGION, sale_price=price,
                            cv_numeric=cv, sale_method=method,
                            sold_date="2026-01-01", import_batch_id=batch.id))


@pytest.mark.parametrize("raw,label,recorded", [
    ("A - Auction", "Auction", True),
    ("P - Private Treaty(Neg.)", NEGOTIATION_LABEL, True),
    ("T - Tender", "Tender", True),
    (None, NEGOTIATION_LABEL, False),      # inferred
    ("", NEGOTIATION_LABEL, False),        # inferred
    ("Z - Something", NEGOTIATION_LABEL, False),
])
def test_method_label_and_whether_it_was_recorded(raw, label, recorded):
    assert _method_label(raw) == label
    assert _method_recorded(raw) is recorded


def test_every_sale_is_accounted_for(db_session, sold_batch):
    db = db_session
    _add(db, sold_batch, 66, "A - Auction", 2_070_000, 2_068_000)
    _add(db, sold_batch, 1, "T - Tender", 3_000_000, 3_200_000)
    _add(db, sold_batch, 40, None, 1_900_000, 1_980_000)
    _add(db, sold_batch, 4, "P - Private Treaty(Neg.)", 1_800_000, 1_900_000)
    db.commit()

    mix, cover = _method_mix_for_suburb(db, sold_batch.id, SUBURB)
    counts = {m.method: m.count for m in mix}

    assert cover.total == 111
    assert cover.shown == 111, "sales went missing between the query and the table"
    assert counts["Auction"] == 66
    assert counts[NEGOTIATION_LABEL] == 44   # 40 inferred + 4 recorded
    assert counts["Tender"] == 1


def test_inferred_rows_are_counted_separately(db_session, sold_batch):
    """The negotiation figure must not look measured when it is mostly inferred."""
    db = db_session
    _add(db, sold_batch, 40, None, 1_900_000, 1_980_000)
    _add(db, sold_batch, 4, "P - Private Treaty(Neg.)", 1_800_000, 1_900_000)
    db.commit()

    _, cover = _method_mix_for_suburb(db, sold_batch.id, SUBURB)
    assert cover.inferred == 40, "inferred methods were folded in without a count"
    assert cover.no_method == 40


def test_unusable_rows_are_reported_not_hidden(db_session, sold_batch):
    db = db_session
    _add(db, sold_batch, 5, "A - Auction", 2_000_000, 2_000_000)
    _add(db, sold_batch, 3, "A - Auction", 1_000_000, None)          # no CV
    _add(db, sold_batch, 2, "A - Auction", 100_000, 3_000_000)       # broken CV
    db.commit()

    mix, cover = _method_mix_for_suburb(db, sold_batch.id, SUBURB)
    assert cover.total == 10
    assert cover.no_cv == 3
    assert cover.implausible_cv == 2
    assert cover.shown == 5
    assert cover.total - cover.shown == 5


def test_a_thin_split_is_flagged(db_session, sold_batch):
    """Under 8 sales a median is one or two properties talking."""
    db = db_session
    _add(db, sold_batch, 2, "T - Tender", 3_000_000, 3_200_000)
    _add(db, sold_batch, 20, "A - Auction", 2_000_000, 2_000_000)
    db.commit()

    mix, _ = _method_mix_for_suburb(db, sold_batch.id, SUBURB)
    by = {m.method: m for m in mix}
    assert by["Tender"].is_thin is True
    assert by["Auction"].is_thin is False


# ── the arm's-length guard on the trends page ────────────────────────────────
def test_a_non_market_sale_cannot_set_a_method_median(db_session, sold_batch):
    """The reported bug: tender showed -93.4% vs CV off a single junk row.

    The property page filtered sales outside 0.3-3.0 of CV; the trends page —
    "Best way to sell here" — did not. A $210k sale against a $3.2M CV is a part
    share or a family transfer, not a tender result, and with one tender sale in
    the suburb that row WAS the median. It then decided which method ranked top.
    """
    from app.routers.properties import suburb_stats

    db = db_session
    _add(db, sold_batch, 30, "A - Auction", 2_070_000, 2_068_000)
    _add(db, sold_batch, 1, "T - Tender", 210_000, 3_200_000)   # sale/CV = 0.066
    db.commit()

    s = suburb_stats(suburb=SUBURB, region=REGION, db=db, from_year=None, to_year=None)
    tender = next((m for m in s.by_method if m.method == "tender"), None)
    assert tender is not None, "the sale should still be counted as a tender sale"
    assert tender.sales == 1
    assert tender.median_vs_cv is None, (
        "a sale at 6% of CV was allowed to become the tender median"
    )


def test_the_suburb_sale_vs_cv_tile_ignores_non_market_sales(db_session, sold_batch):
    db = db_session
    _add(db, sold_batch, 30, "A - Auction", 2_070_000, 2_068_000)
    _add(db, sold_batch, 5, "A - Auction", 210_000, 3_200_000)   # junk
    db.commit()

    from app.routers.properties import suburb_stats
    s = suburb_stats(suburb=SUBURB, region=REGION, db=db, from_year=None, to_year=None)
    assert s.sale_vs_cv is not None
    # The real sales run at ~+0.1% vs CV; the junk would drag it far negative.
    assert -0.05 < s.sale_vs_cv < 0.05, f"junk sales moved the tile to {s.sale_vs_cv}"


# ── the bucket the trends panel actually uses ────────────────────────────────
@pytest.mark.parametrize("raw,bucket", [
    ("A - Auction", "auction"),
    ("T - Tender", "tender"),
    # The value this data really carries. It contains "neg." but not "negoti",
    # so it matched nothing and returned None — every private-treaty sale was
    # dropped from the panel, and "Price by Negotiation" could not appear at all.
    ("P - Private Treaty(Neg.)", "negotiation"),
    ("Private Treaty", "negotiation"),
    ("By Negotiation", "negotiation"),
    # No method recorded: counted as private treaty, as _method_label does.
    (None, "negotiation"),
    ("", "negotiation"),
    # "p" is Private Treaty in the source's coding; it used to say "fixed" here
    # while _METHOD_LABELS read the same letter as Price by Negotiation.
    ("p", "negotiation"),
    ("a", "auction"),
    ("D - Deadline", "deadline"),
])
def test_method_bucket_recognises_the_real_values(raw, bucket):
    from app.routers.properties import _method_bucket

    assert _method_bucket(raw) == bucket


def test_negotiation_appears_on_the_trends_panel(db_session, sold_batch):
    """The reported bug: only Auction and Tender, never Price by Negotiation."""
    from app.routers.properties import suburb_stats

    db = db_session
    _add(db, sold_batch, 62, "A - Auction", 2_290_000, 2_236_000)
    _add(db, sold_batch, 30, "P - Private Treaty(Neg.)", 1_900_000, 1_960_000)
    _add(db, sold_batch, 20, None, 1_850_000, 1_910_000)
    _add(db, sold_batch, 1, "T - Tender", 700_000, 1_450_000)
    db.commit()

    s = suburb_stats(suburb=SUBURB, region=REGION, db=db, from_year=None, to_year=None)
    by = {m.method: m for m in s.by_method}

    assert "negotiation" in by, "Price by Negotiation is still missing from the panel"
    assert by["negotiation"].sales == 50, "recorded and unrecorded sales were not both counted"
    assert by["auction"].sales == 62
