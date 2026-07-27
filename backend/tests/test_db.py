from sqlalchemy import Column, Integer, String, text

from app.models import Base


def test_db_session_executes_query(db_session):
    result = db_session.execute(text("SELECT 1")).scalar_one()
    assert result == 1


class _FixtureProbe(Base):
    """Table used only to verify the db_session fixture's isolation guarantees.

    Not a real domain model — exists solely so the tests below can prove that
    a commit() inside a test does not leak rows into other tests or into the
    real streamclip_test database.
    """

    __tablename__ = "_fixture_probe"

    id = Column(Integer, primary_key=True)
    value = Column(String, nullable=False)


def test_db_session_commit_is_contained_by_fixture(db_session):
    # Simulates what model/service tests will do starting with Task 3: commit
    # inside the test. If the fixture didn't join the session to the outer
    # transaction via a savepoint, this commit would end the outer
    # transaction early and the row would survive teardown's rollback.
    db_session.add(_FixtureProbe(value="should-not-persist"))
    db_session.commit()
    assert db_session.query(_FixtureProbe).count() == 1


def test_db_session_rolls_back_previous_tests_commit(db_session):
    # Runs in a fresh db_session fixture instance. If the previous test's
    # commit had escaped the outer transaction, this row would still be here.
    assert db_session.query(_FixtureProbe).count() == 0
