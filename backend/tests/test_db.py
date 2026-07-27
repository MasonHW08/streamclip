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
    # Documents the desired end-to-end behavior: commit inside a test must
    # not leak rows past teardown. NOTE: on this SQLAlchemy version (2.0.51),
    # this pair of tests does NOT by itself discriminate fixed-vs-buggy
    # fixture code — see test_db_session_commit_keeps_a_savepoint_active
    # below for the test that actually does. Kept as a readable integration-
    # level statement of the contract other tasks rely on.
    db_session.add(_FixtureProbe(value="should-not-persist"))
    db_session.commit()
    assert db_session.query(_FixtureProbe).count() == 1


def test_db_session_rolls_back_previous_tests_commit(db_session):
    # Runs in a fresh db_session fixture instance. Passes regardless of the
    # savepoint fix on this SQLAlchemy version (see note above) because
    # SQLAlchemy's own "rollback_only" join mode already suppresses the real
    # COMMIT when no savepoint is present. Kept for readability/documentation
    # of intent, not as the proof of the fix.
    assert db_session.query(_FixtureProbe).count() == 0


def test_db_session_commit_keeps_a_savepoint_active(db_session):
    # This is the test that actually discriminates fixed-vs-buggy fixture
    # code. The fixture joins the session to the outer transaction by
    # opening a SAVEPOINT (connection.begin_nested()) and restarting it via
    # an after_transaction_end listener whenever it ends (e.g. on commit).
    # Without that logic, SQLAlchemy's default join_transaction_mode falls
    # back to "rollback_only" (no nested transaction present), and
    # connection.in_nested_transaction() stays False before and after
    # commit(). With the fix, a savepoint is always active, ready to be
    # rolled back independently in teardown.
    connection = db_session.connection()
    db_session.commit()
    assert connection.in_nested_transaction() is True
