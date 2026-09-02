"""One-shot DB bootstrap, run before uvicorn on every boot.

Heals a database left inconsistent by an earlier repo upload (production was
stamped at revision `a9f4c2e81b30`, which this codebase no longer contains, and
that migration also created columns as NOT NULL that these models treat as
optional). Symptoms it fixes:

  * `alembic upgrade head` aborting with "Can't locate revision" → app never
    started (took logins + all data down).
  * every ingest-job insert failing with NotNullViolation (upload/enrich/publish
    500s) because a column the models leave null is NOT NULL in the DB.

What it does, without invoking alembic:
  1. Ensure the columns this codebase added exist (idempotent ADD COLUMN).
  2. Reconcile NULLability to the models everywhere: for every column the models
     declare nullable, DROP NOT NULL in the DB if it's currently NOT NULL. This
     covers ANY column that old migration over-constrained, not just a guessed
     few.
  3. Reset `alembic_version` to this codebase's head, ignoring the unknown rev.

Each statement runs in its own transaction so one failure can't poison the rest,
and nothing here ever aborts startup — a running app beats a dead one.
"""
from __future__ import annotations

import sys

from sqlalchemy import inspect, text

import app.models  # noqa: F401  — populate Base.metadata
from app.db import Base, engine

HEAD_REVISION = "a1c2e3d4f5b6"

# New columns this codebase added that an out-of-sync DB may be missing.
_ADD_COLUMNS = (
    "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS rows_filled INTEGER",
    "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS rows_missed INTEGER",
    "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS result_json TEXT",
    "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS last_progress_at TIMESTAMPTZ",
    "ALTER TABLE properties_for_sale ADD COLUMN IF NOT EXISTS lots_override DOUBLE PRECISION",
    # bug_reports gained automatic capture after it first shipped.
    "ALTER TABLE bug_reports ADD COLUMN IF NOT EXISTS source VARCHAR(16) DEFAULT 'manual'",
    "ALTER TABLE bug_reports ADD COLUMN IF NOT EXISTS occurrences INTEGER DEFAULT 1",
    "ALTER TABLE bug_reports ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ",
    "ALTER TABLE bug_reports ADD COLUMN IF NOT EXISTS fingerprint VARCHAR(200)",
    # Trade Me's own figure, filled from their sales export (app/trademe.py).
    *(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {c}"
      for t in ("properties_for_sale", "properties_sold", "properties_rent")
      for c in ("tm_valuation DOUBLE PRECISION",
                "tm_valuation_low DOUBLE PRECISION",
                "tm_valuation_high DOUBLE PRECISION",
                "tm_valuation_date VARCHAR(32)")),
)


def _run(sql: str) -> bool:
    """Run one statement in its own transaction; log and move on if it can't."""
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
        return True
    except Exception as e:
        print(f"[db_bootstrap] skip: {sql[:60]}… -> {type(e).__name__}", flush=True)
        return False


def ensure_columns(bind=None) -> list[str]:
    """Add every column the models declare that its table does not have.

    create_all() builds missing TABLES and will not touch one that exists, so a
    column added to an existing model has always needed a migration of its own.
    That gap took the site down: four tm_valuation columns were added to the
    property models and to the hand-written list below, which lives in a module
    the production start command does not run. Every ORM query selects every
    mapped column, so the moment the app booted, /api/properties,
    /api/properties/suburb-stats and /api/dashboards/today all answered

        ProgrammingError: column properties_for_sale.tm_valuation does not exist

    Three pages down, from a column nobody had used yet.

    Derived from the models rather than hand-listed, so the next column added
    cannot be forgotten. Columns are added NULLABLE whatever the model says: a
    table with rows in it cannot accept a NOT NULL column with no default, and
    a live site matters more than a constraint. _reconcile_nullability() already
    treats the models as the authority on that.
    """
    eng = bind or engine
    try:
        insp = inspect(eng)
        tables = set(insp.get_table_names())
    except Exception as e:
        print(f"[db_bootstrap] could not inspect DB: {type(e).__name__}: {e}", flush=True)
        return []

    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue                      # create_all's job, not this one
        try:
            have = {c["name"] for c in insp.get_columns(table.name)}
        except Exception:
            continue
        for col in table.columns:
            if col.name in have:
                continue
            try:
                ddl = col.type.compile(eng.dialect)
            except Exception:
                continue                  # a type this dialect cannot spell
            if _run(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'):
                added.append(f"{table.name}.{col.name}")
    return added


def _reconcile_nullability() -> int:
    """For every model column that is nullable, DROP NOT NULL in the DB where it's
    currently NOT NULL. Aligns the DB to the models so inserts that leave optional
    columns null never violate a stray NOT NULL constraint. Returns count fixed."""
    fixed = 0
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
    except Exception as e:
        print(f"[db_bootstrap] could not inspect DB: {type(e).__name__}: {e}", flush=True)
        return 0

    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        try:
            db_cols = {c["name"]: c for c in insp.get_columns(table.name)}
        except Exception:
            continue
        for col in table.columns:
            db_col = db_cols.get(col.name)
            if col.nullable and db_col is not None and db_col.get("nullable") is False:
                _run(f'ALTER TABLE "{table.name}" ALTER COLUMN "{col.name}" DROP NOT NULL')
                fixed += 1
    return fixed


def _widen_strings() -> int:
    """Grow every String column the models made longer than the DB has it.

    ensure_columns() adds columns and _reconcile_nullability() relaxes NOT NULL;
    neither can make an existing column longer, and a String that is too short
    does not degrade — on Postgres it raises StringDataRightTruncation and takes
    down the write that carries it. The publish stage was killed this way once
    already, by a status string that outgrew String(64).

    The one that prompted this: `best_strategy` is String(32), and
    "Retain house + sell new sections" is exactly 32 characters — no headroom at
    all. Adding "Demolish and subdivide into 5 sections" at 38 would have failed
    every pricing run on Postgres and passed every test on SQLite, which does
    not enforce the limit. That combination is the worst one available.

    Derived from the models rather than hand-listed, for the same reason
    ensure_columns is: the next column somebody lengthens cannot be forgotten.
    Only ever grows a column — never shrinks one, which would truncate real
    data. SQLite ignores VARCHAR lengths entirely, so this is a no-op there and
    the statement is allowed to fail harmlessly.
    """
    grown = 0
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
    except Exception as e:
        print(f"[db_bootstrap] could not inspect DB: {type(e).__name__}: {e}", flush=True)
        return 0

    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        try:
            db_cols = {c["name"]: c for c in insp.get_columns(table.name)}
        except Exception:
            continue
        for col in table.columns:
            want = getattr(col.type, "length", None)
            db_col = db_cols.get(col.name)
            if want is None or db_col is None:
                continue
            have = getattr(db_col.get("type"), "length", None)
            if have is None or have >= want:
                continue
            if _run(f'ALTER TABLE "{table.name}" ALTER COLUMN "{col.name}" '
                    f'TYPE VARCHAR({int(want)})'):
                print(f"[db_bootstrap] widened {table.name}.{col.name} "
                      f"{have} -> {want}", flush=True)
                grown += 1
    return grown


def main() -> int:
    # Create any tables the models define that don't exist yet. On an existing
    # production DB this is a no-op (every table is already there); on a FRESH or
    # empty database it builds the whole schema. This replaces `alembic upgrade
    # head` for table creation — alembic was aborting on a phantom revision, and
    # skipping it left an empty DB with no tables (the app then crashed querying
    # `users`). create_all never drops or alters existing tables, so it's safe on
    # a populated DB. Never aborts startup.
    try:
        Base.metadata.create_all(engine)
        print("[db_bootstrap] schema ensured (create_all)", flush=True)
    except Exception as e:
        print(f"[db_bootstrap] create_all skipped: {type(e).__name__}: {e}", flush=True)

    for stmt in _ADD_COLUMNS:
        _run(stmt)

    # Everything _ADD_COLUMNS lists by hand, derived from the models instead.
    added = ensure_columns()
    if added:
        print(f"[db_bootstrap] added {len(added)} missing column(s): "
              f"{', '.join(added)}", flush=True)

    fixed = _reconcile_nullability()

    # After the columns exist, before anything writes to them.
    grown = _widen_strings()
    if grown:
        print(f"[db_bootstrap] widened {grown} string column(s)", flush=True)

    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"
            ))
            conn.execute(text("DELETE FROM alembic_version"))
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
                         {"v": HEAD_REVISION})
        print(f"[db_bootstrap] ok — columns ensured, {fixed} NOT NULL constraint(s) "
              f"relaxed to match models, alembic_version = {HEAD_REVISION}", flush=True)
    except Exception as e:
        print(f"[db_bootstrap] WARNING (continuing to start server): "
              f"{type(e).__name__}: {e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
