"""Password hashing roundtrip and tamper-resistance."""

from enlace.auth.passwords import hash_password, verify_password


def test_hash_verify_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password(h, "correct horse battery staple") is True


def test_wrong_password_rejected():
    h = hash_password("secret")
    assert verify_password(h, "guess") is False


def test_tampered_hash_rejected():
    h = hash_password("secret")
    # Flip a character in the hash to break it.
    tampered = h[:-1] + ("a" if h[-1] != "a" else "b")
    assert verify_password(tampered, "secret") is False


def test_nonsense_hash_rejected():
    assert verify_password("not-a-real-hash", "secret") is False


def test_hashes_differ_for_same_password():
    """Salting means two hashes of the same password should differ."""
    assert hash_password("x") != hash_password("x")
