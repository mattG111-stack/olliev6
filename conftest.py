"""Shared test fixtures.

The pricing tests are pure functions and never needed a database. The ingest
tests do, so this sets one up: a throwaway SQLite file, rebuilt per test, so no
test can pass because of a row another one left behind.

The environment has to be set BEFORE app.db is imported — the engine is built at
import time from settings.database_url, so importing first and setting after
would point every test at whatever DATABASE_URL the shell happened to carry
(in practice, production).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TEST_DB = Path(tempfile.gettempdir()) / "ollie_pytest.db"

os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TEST_DB}")
os.environ.setdefault("JWT_SECRET", "not-a-secret-tests")
os.environ.setdefault("CORS_ORIGINS", "*")


@pytest.fixture()
def db_session():
    """A session against an empty schema. Dropped and rebuilt for each test."""
    from app.db import Base, SessionLocal, engine
    from app import models  # noqa: F401  — registers the tables on Base

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
