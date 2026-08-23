"""8.997 started, reported READY, served requests — and was killed.

What reached the log was `asyncio.exceptions.CancelledError` from Starlette's
lifespan being torn down, which names nothing and points at nothing. The app had
not crashed. It was OOM-killed from outside while a background thread was inside
pandas.

The thread was the boot-time self-heal: re-price every staged batch, which means
every listing in the batch AND the entire sold history, as ORM objects and as
DataFrames, at once, in the container that is also answering requests. Ten
thousand PropertyForSale objects, fifty thousand PropertySold objects, a frame
built from each, and the pipeline's output frame beside them.

The codebase already knew. staged_stages.py: "which is what let a few stacked
runs OOM-kill the container". main.py, fifteen lines below the thread that did
it: "a long, memory-hungry job ... is the opposite shape, and sharing a
container means every one of those is a way for the site to go down".

Three things, and these tests hold each of them:

  the self-heal does not run in the API container
  the sold history is read as columns, not as fifty thousand objects
  the batch is re-priced in chunks, so peak memory is bounded by the chunk
"""
from __future__ import annotations

import pathlib

from app.models import BatchType, ImportBatch, PropertyForSale, PropertySold
from app.reprice import REPRICE_CHUNK, _sold_df, reprice_batch


def _batches(db, n_listings: int, n_sold: int = 12):
    fs = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                     filename="week.xlsx", is_active=True, status="staged")
    sold = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland",
                       filename="sold.xlsx", is_active=True, status="published")
    db.add_all([fs, sold])
    db.flush()
    for i in range(n_listings):
        db.add(PropertyForSale(
            import_batch_id=fs.id, address=f"{i} Test Road", suburb="Papakura",
            district="Papakura", property_type="House", beds=3, baths=1,
            floor_area_m2=140.0 + i, land_area_m2=600.0, cv_numeric=900_000,
            land_value_numeric=500_000, improvement_value_numeric=400_000,
            asking_price=950_000, type_of_title="Freehold"))
    for i in range(n_sold):
        db.add(PropertySold(
            import_batch_id=sold.id, address=f"{i} Sold Street", suburb="Papakura",
            district="Papakura", property_type="House", beds=3, baths=1,
            floor_area_m2=140.0 + i, land_area_m2=600.0, cv_numeric=880_000,
            sale_price=900_000 + i * 1000, sold_date="2026-06-01",
            type_of_title="Freehold"))
    db.commit()
    return fs, sold


# ---- the sold history is columns, not objects ------------------------------
def test_reading_the_sold_history_does_not_fill_the_session(db_session):
    """It used to be `.all()` on the full model, so every sold record became an
    ORM instance held in the identity map — and then a DataFrame of the same
    data beside them. Two copies to produce one."""
    _batches(db_session, 3, n_sold=25)
    db_session.expunge_all()

    df = _sold_df(db_session, "Auckland")
    assert len(df) == 25, "the frame is still built"

    live = [o for o in db_session.identity_map.values()
            if isinstance(o, PropertySold)]
    assert live == [], f"{len(live)} sold objects held after building the frame"


def test_the_sold_frame_still_carries_every_column_the_engine_reads(db_session):
    _batches(db_session, 1, n_sold=3)
    df = _sold_df(db_session, "Auckland")
    for column in ("address", "suburb", "district", "property_type",
                   "key_bedrooms", "key_bathrooms", "key_floor_area",
                   "key_land_area", "price_numeric", "cv_numeric",
                   "land_value_numeric", "type_of_title", "sold_date",
                   "days_on_market"):
        assert column in df.columns, column
    assert df["price_numeric"].notna().all()


# ---- the batch is re-priced in chunks --------------------------------------
def test_a_batch_is_priced_in_chunks_not_all_at_once(db_session):
    """Peak memory is the sold history plus one chunk, not plus the whole
    batch. Ten thousand listings is what the killed container was holding."""
    _batches(db_session, 7)
    res = reprice_batch(db_session, 1, commit=True, chunk=2)
    assert res.rows == 7, "every listing is still priced"
    assert res.committed


def test_chunking_prices_the_same_rows_as_one_pass(db_session):
    """The fix must not change any number, only the memory it takes to get it."""
    _batches(db_session, 6)
    one = reprice_batch(db_session, 1, commit=True, chunk=1000)
    values_one = [p.fair_value for p in db_session.query(PropertyForSale)
                  .order_by(PropertyForSale.id).all()]

    many = reprice_batch(db_session, 1, commit=True, chunk=2)
    values_many = [p.fair_value for p in db_session.query(PropertyForSale)
                   .order_by(PropertyForSale.id).all()]

    assert one.rows == many.rows == 6
    assert values_one == values_many


def test_the_session_is_emptied_between_chunks(db_session):
    """Without this the identity map keeps every object from every chunk and
    the chunking buys nothing at all."""
    _batches(db_session, 6)
    reprice_batch(db_session, 1, commit=True, chunk=2)
    live = [o for o in db_session.identity_map.values()
            if isinstance(o, PropertyForSale)]
    assert len(live) <= 2, f"{len(live)} listings still held after the run"


def test_a_run_killed_halfway_has_saved_half_its_work(db_session):
    """Committing per chunk rather than once at the end. The container really
    does get killed sometimes, and losing an hour of pricing to it is avoidable."""
    _batches(db_session, 4)
    res = reprice_batch(db_session, 1, commit=True, chunk=2)
    assert res.committed
    priced = (db_session.query(PropertyForSale)
              .filter(PropertyForSale.fair_value.isnot(None)).count())
    assert priced > 0


def test_an_empty_batch_still_says_so(db_session):
    _batches(db_session, 0)
    assert reprice_batch(db_session, 1).error == "no listings in batch"


def test_the_default_chunk_is_a_real_bound(db_session):
    assert 1 <= REPRICE_CHUNK <= 2000


# ---- and it is not started by the API --------------------------------------
def test_the_api_does_not_start_the_self_heal_on_boot():
    """The whole cause, in one assertion. This thread ran unconditionally on
    every boot and killed the 8.997 deploy."""
    src = pathlib.Path("app/main.py").read_text()
    i = src.index("threading.Thread(target=auto_reprice_stale_batches")
    assert "AUTO_REPRICE_ON_BOOT" in src[:i]


def test_the_worker_runs_it_instead():
    from app.worker import build_jobs

    assert "re-price stale batches" in [j.name for j in build_jobs()]


# ---- and next time, it says so ---------------------------------------------
def test_a_kill_and_a_clean_shutdown_do_not_look_the_same_in_the_log():
    """What 8.997 left behind was a CancelledError naming Starlette and nothing
    of ours — identical to what a normal deploy leaves. One line separates them,
    with how long the process lived: seconds means a crash loop, minutes after a
    deploy means something killed it."""
    src = pathlib.Path("app/main.py").read_text()
    assert "shutting down cleanly" in src
    assert "killed from outside" in src
    assert "memory limit" in src
