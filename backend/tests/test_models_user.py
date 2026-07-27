from app.models.user import User


def test_create_user(db_session):
    user = User(email="team@streamclip.co", hashed_password="hashed", role="admin")
    db_session.add(user)
    db_session.commit()

    assert user.id is not None
    assert user.role == "admin"
