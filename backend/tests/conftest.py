import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://streamclip:streamclip@localhost:5432/streamclip_test",
)
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.models import Base  # noqa: E402

get_settings.cache_clear()
_settings = get_settings()
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
    yield session
    session.close()
    transaction.rollback()
    connection.close()
