"""Is this deploy actually able to serve? Asked and answered in one place.

Three days of instability, four distinct causes, and not one of them announced
itself. Each was found by the site being down and someone reading a log:

    a library stopped being installed because an SDK we depend on released a
    major version and dropped it

    a column existed in the models and not in the database, so every query
    selecting that table failed

    two environment variables were unset, and the process said so 380 times in
    a validation traceback

    the builder could not tell what language the project was

None of those are the same bug. What they share is that the system had the
information to say what was wrong and said nothing useful — so every one cost a
round of guessing before the fix, which usually took a minute.

This checks the things that have actually broken, in the order they break, and
says so in one line each. It is not a test suite; it is the question "can this
serve requests right now", asked at boot and available at /health/ready.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True          # False = degraded, still able to serve

    def line(self) -> str:
        mark = "ok  " if self.ok else ("FAIL" if self.fatal else "warn")
        return f"[{mark}] {self.name}{': ' + self.detail if self.detail else ''}"


def _settings() -> Check:
    """Config is loaded at import, so reaching here means it worked."""
    try:
        from .config import settings

        missing = [n for n in ("database_url", "jwt_secret")
                   if not getattr(settings, n, None)]
        if missing:
            return Check("settings", False,
                         f"not set: {', '.join(n.upper() for n in missing)}")
        return Check("settings", True)
    except SystemExit:
        return Check("settings", False, "required environment variables are not set")
    except Exception as e:                        # noqa: BLE001
        return Check("settings", False, f"{type(e).__name__}: {e}")


def _database() -> Check:
    from sqlalchemy import text

    from .db import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return Check("database", True, engine.dialect.name)
    except Exception as e:                        # noqa: BLE001
        return Check("database", False, f"{type(e).__name__}: {str(e)[:160]}")


def _schema() -> Check:
    """Every column the models declare, present in the database.

    The failure this catches took four pages down at once: a column added to a
    model, its migration written into a module the start command does not run,
    and every ORM query against that table answering ProgrammingError.
    """
    try:
        from sqlalchemy import inspect

        from . import models  # noqa: F401 — importing is what fills the metadata
        from .db import Base, engine

        insp = inspect(engine)
        tables = set(insp.get_table_names())
        missing: list[str] = []
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                missing.append(f"{table.name} (whole table)")
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            missing += [f"{table.name}.{c.name}" for c in table.columns
                        if c.name not in have]
        if missing:
            return Check("schema", False,
                         f"{len(missing)} missing — {', '.join(missing[:6])}"
                         + ("…" if len(missing) > 6 else ""))
        return Check("schema", True, f"{len(tables)} tables")
    except Exception as e:                        # noqa: BLE001
        return Check("schema", False, f"{type(e).__name__}: {str(e)[:160]}")


def _imports() -> Check:
    """The libraries our own modules import at request time.

    httpx was never declared as a requirement and arrived as a dependency of the
    Anthropic SDK. That SDK released 1.0.0, moved to httpx2, and the app could no
    longer start. Nothing in the codebase had changed.
    """
    import importlib

    needed = ("httpx", "pandas", "sqlalchemy", "fastapi", "jose", "passlib")
    gone = []
    for name in needed:
        try:
            importlib.import_module(name)
        except Exception:                          # noqa: BLE001
            gone.append(name)
    if gone:
        return Check("imports", False, f"not installed: {', '.join(gone)}")
    return Check("imports", True, f"{len(needed)} checked")


def _data() -> Check:
    """Is there anything to show? Not fatal — an empty database still serves."""
    try:
        from .db import SessionLocal
        from .models import BatchType, ImportBatch

        db = SessionLocal()
        try:
            live = (db.query(ImportBatch)
                    .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                            ImportBatch.is_active.is_(True)).count())
        finally:
            db.close()
        if not live:
            return Check("data", False, "no live for-sale batch — pages will be empty",
                         fatal=False)
        return Check("data", True, f"{live} live batch(es)", fatal=False)
    except Exception as e:                        # noqa: BLE001
        return Check("data", False, f"{type(e).__name__}: {str(e)[:120]}", fatal=False)


def _verification() -> Check:
    """Are the hand-run verification passes actually present on the live batch?

    Two of the publish gate's hold rules read columns that nothing in the
    load → enrich → price → publish flow ever writes:

        land_area_flag  <- scripts/verify_batch.py   (re-reads each listing's
                           own page; 139 Long Drive was stored at 5,665 m²
                           against a real 416 m², a $5M phantom "gem")
        cv_flag         <- scripts/reconcile_cv.py   (our CV vs homes.co.nz's,
                           with the asking price as referee)

    Skip a script and its rule is a no-op — which looks exactly like protection
    that is working. Nothing was wrong with the rules; what was wrong is that
    their absence was silent. This says so out loud.

    Not fatal: a batch published without them still serves. It is just less
    checked than the code reads as, and now that says so.
    """
    try:
        from sqlalchemy import func

        from .db import SessionLocal
        from .models import BatchType, ImportBatch, PropertyForSale

        db = SessionLocal()
        try:
            batch = (db.query(ImportBatch.id)
                     .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                             ImportBatch.is_active.is_(True))
                     .order_by(ImportBatch.id.desc()).first())
            if not batch:
                return Check("verification", True, "no live batch to check",
                             fatal=False)
            rows = (db.query(
                func.count(PropertyForSale.id),
                func.count(PropertyForSale.land_area_flag),
                func.count(PropertyForSale.cv_flag))
                .filter(PropertyForSale.import_batch_id == batch[0]).one())
        finally:
            db.close()

        total, land, cv = (int(x or 0) for x in rows)
        if not total:
            return Check("verification", True, "live batch is empty", fatal=False)
        missing = []
        if not land:
            missing.append("land areas unverified (scripts/verify_batch.py)")
        if not cv:
            missing.append("CVs unreconciled (scripts/reconcile_cv.py)")
        if missing:
            return Check("verification", False, "; ".join(missing), fatal=False)
        return Check("verification", True,
                     f"land {land}/{total}, cv {cv}/{total}", fatal=False)
    except Exception as e:                        # noqa: BLE001
        return Check("verification", False, f"{type(e).__name__}: {str(e)[:120]}",
                     fatal=False)


def check() -> list[Check]:
    """Every check, in the order things break."""
    out = [_settings()]
    if not out[0].ok:
        return out                                # nothing else can work
    out.append(_imports())
    out.append(_database())
    if out[-1].ok:
        out.append(_schema())
        out.append(_data())
        out.append(_verification())
    return out


def ready(checks: list[Check] | None = None) -> bool:
    return all(c.ok for c in (checks or check()) if c.fatal)


def report(checks: list[Check] | None = None) -> str:
    checks = checks or check()
    head = "READY" if ready(checks) else "NOT READY"
    return "\n".join([f"preflight: {head}"] + ["  " + c.line() for c in checks])


def main() -> int:
    checks = check()
    print(report(checks), flush=True)
    return 0 if ready(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
