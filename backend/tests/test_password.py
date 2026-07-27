from app.services.password import hash_password, verify_password


def test_hash_and_verify_roundtrip():
    hashed = hash_password("correct-password")
    assert hashed != "correct-password"
    assert verify_password("correct-password", hashed)
    assert not verify_password("wrong-password", hashed)
