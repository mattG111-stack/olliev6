from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


def _normalise_db_url(url: str) -> str:
    """Force the psycopg v3 driver since we install `psycopg[binary]` (v3) in requirements.txt.

    SQLAlchemy's default for a bare `postgresql://` is psycopg v2, which we don't install,
    so the import fails on startup. Rewrite to `postgresql+psycopg://` to be explicit.
    Also handles the legacy `postgres://` scheme some hosts (Heroku etc.) emit.
    """
    if url.startswith("postgresql+"):
        return url  # already explicit (e.g. postgresql+psycopg, postgresql+asyncpg)
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


DB_URL = _normalise_db_url(settings.database_url)
engine = create_engine(DB_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# SQLite ignores every foreign key unless asked not to, and the tests run on
# SQLite while production is Postgres. That gap is not cosmetic: a delete that
# leaves a row pointing at a vanished user passes locally and fails in
# production with a constraint violation the tests could never have seen. It is
# exactly how `app_settings.updated_by` shipped uncleared and made deleting an
# admin answer 500. Turning enforcement on makes the test database refuse the
# same things the real one refuses.
if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _sqlite_enforce_foreign_keys(dbapi_connection, _record):  # pragma: no cover
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
