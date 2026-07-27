"""The test suite drops the whole schema, so it must never point at the dev database."""

import pytest
from conftest import require_test_database

DEV_URL = "postgresql+psycopg://streamclip:streamclip@localhost:5432/streamclip"
TEST_URL = "postgresql+psycopg://streamclip:streamclip@localhost:5432/streamclip_test"


def test_dev_database_url_is_refused():
    with pytest.raises(RuntimeError, match="Refusing to run tests against database"):
        require_test_database(DEV_URL)


def test_test_database_url_is_allowed():
    require_test_database(TEST_URL)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://u:p@prod.example.com:5432/streamclip",
        "postgresql+psycopg://u:p@localhost:5432/production",
        "postgresql+psycopg://u:p@localhost:5432/",
    ],
)
def test_non_test_database_names_are_refused(url):
    with pytest.raises(RuntimeError):
        require_test_database(url)


def test_error_message_is_actionable():
    with pytest.raises(RuntimeError) as excinfo:
        require_test_database(DEV_URL)
    message = str(excinfo.value)
    assert "'streamclip'" in message
    assert "DATABASE_URL" in message
    assert "streamclip_test" in message
