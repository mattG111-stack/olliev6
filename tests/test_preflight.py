"""Every failure should name itself.

Three days, four outages, four different causes — and not one of them said what
was wrong. Each was found by the site being down and someone reading a log:

  * a library stopped being installed, because an SDK we depend on released a
    major version and moved off it
  * a column was in the models and not in the database, so every query against
    that table answered ProgrammingError and four pages went at once
  * two environment variables were unset, and the process said so 380 times in
    a pydantic traceback
  * the builder could not tell what language the project was

None of those is the same bug, and none of them was hard to fix once known. What
they shared was silence: the system had the information and reported it in a
form nobody could act on. That is the thing worth fixing, because the next
outage will be a fifth cause nobody has thought of.

So these tests are about what happens when things are wrong, not when they work.
"""
from __future__ import annotations

from app.preflight import Check, check, ready, report


def test_a_healthy_deploy_reads_ready(db_session):
    checks = check()
    assert ready(checks), report(checks)


def test_the_report_names_every_check(db_session):
    text = report()
    for name in ("settings", "imports", "database", "schema"):
        assert name in text, text


def test_a_missing_column_makes_it_not_ready(db_session):
    """The tm_valuation outage, as a check rather than four broken pages."""
    from sqlalchemy import text as sql

    with db_session.bind.begin() as conn:
        conn.execute(sql("ALTER TABLE properties_for_sale DROP COLUMN tm_valuation"))

    checks = check()
    schema = next(c for c in checks if c.name == "schema")
    assert not schema.ok
    assert "tm_valuation" in schema.detail
    assert not ready(checks)


def test_an_empty_database_is_a_warning_not_a_failure(db_session):
    """No listings loaded yet is a state to be in, not a broken deploy."""
    checks = check()
    data = next(c for c in checks if c.name == "data")
    assert data.fatal is False
    assert ready(checks), "an empty database reported the app as unable to serve"


def test_a_check_that_fails_says_which_one_and_why():
    bad = Check("database", False, "OperationalError: could not connect")
    assert bad.line().startswith("[FAIL] database:")
    assert "could not connect" in bad.line()


def test_a_missing_environment_variable_is_one_sentence_not_a_traceback():
    """What the crash loop printed 380 times, said once and in English.

    Run in a subprocess with the variables removed, because config is read at
    import and this codebase has already imported it.
    """
    import subprocess
    import sys

    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp"}
    out = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        capture_output=True, text=True, env=env, cwd=".", timeout=120)

    assert out.returncode != 0, "a missing DATABASE_URL started anyway"
    said = out.stdout + out.stderr
    assert "DATABASE_URL" in said and "JWT_SECRET" in said, said[:400]
    assert "CANNOT START" in said, said[:400]
    # The thing that made the old log useless: forty lines of pydantic internals
    # to say two variables are unset.
    assert "pydantic_core" not in said, said[:400]


def test_an_unverified_batch_says_which_pass_did_not_run(db_session):
    """Two hold rules read columns only a hand-run script ever writes.

    land_area_flag comes from scripts/verify_batch.py, cv_flag from
    scripts/reconcile_cv.py. Skip either and its rule in release._hold_reason is
    a no-op — indistinguishable, from the outside, from protection that is
    working. That is the failure mode this whole module exists for.
    """
    from app.models import BatchType, ImportBatch, PropertyForSale

    batch = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                        filename="unverified.xlsx", is_active=True, status="published")
    db_session.add(batch)
    db_session.flush()
    db_session.add(PropertyForSale(
        import_batch_id=batch.id, address="1 Unchecked Way", suburb="Papakura",
        property_type="House", floor_area_m2=140.0, is_held=False))
    db_session.commit()

    checks = check()
    v = next(c for c in checks if c.name == "verification")
    assert not v.ok
    assert "verify_batch" in v.detail and "reconcile_cv" in v.detail, v.detail
    # Less checked than the code reads as is not the same as unable to serve.
    assert v.fatal is False
    assert ready(checks), "an unverified batch reported the app as unable to serve"


def test_a_verified_batch_reports_its_coverage(db_session):
    from app.models import BatchType, ImportBatch, PropertyForSale

    batch = ImportBatch(batch_type=BatchType.FOR_SALE.value, region="Auckland",
                        filename="verified.xlsx", is_active=True, status="published")
    db_session.add(batch)
    db_session.flush()
    db_session.add(PropertyForSale(
        import_batch_id=batch.id, address="2 Checked Way", suburb="Papakura",
        property_type="House", floor_area_m2=140.0, is_held=False,
        land_area_flag="ok", cv_flag="ok"))
    db_session.commit()

    v = next(c for c in check() if c.name == "verification")
    assert v.ok, v.detail
    assert "1/1" in v.detail, v.detail
