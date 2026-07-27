import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://streamclip:streamclip@localhost:5432/streamclip_test",
)
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")

import pytest  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.models import Base  # noqa: E402


def require_test_database(database_url: str) -> None:
    """Refuse to run against anything that isn't obviously a test database.

    The schema fixture below runs create_all/drop_all. `setdefault` above means an
    already-exported DATABASE_URL in the shell wins — so a leftover
    `export DATABASE_URL=...streamclip` from a manual alembic run would point this at
    the DEV database and drop every table in it.
    """
    db_name = make_url(database_url).database or ""
    if "test" not in db_name:
        raise RuntimeError(
            f"Refusing to run tests against database {db_name!r} (resolved from "
            f"DATABASE_URL): the test suite creates and drops the whole schema, and "
            f"this does not look like a test database. Unset any exported DATABASE_URL, "
            f"or point it at a database whose name contains 'test' (e.g. "
            f"postgresql+psycopg://streamclip:streamclip@localhost:5432/streamclip_test)."
        )


get_settings.cache_clear()
_settings = get_settings()
require_test_database(_settings.database_url)

engine = create_engine(_settings.database_url)


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture()
def db_session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans):
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    yield session

    session.close()
    transaction.rollback()
    connection.close()
