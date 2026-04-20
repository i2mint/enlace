"""Signed cookie helpers built on itsdangerous.

We wrap ``URLSafeTimedSerializer`` so signing_key rotation and ``max_age`` checks
are centralised; callers never touch itsdangerous directly.
"""

from __future__ import annotations

from typing import Optional


def _serializer(signing_key: str, salt: str = "enlace-cookie"):
    try:
        from itsdangerous import URLSafeTimedSerializer  # type: ignore
    except ImportError as e:
        raise ImportError(
            "itsdangerous is required for signed cookies. "
            "Install it via `pip install enlace[auth]`."
        ) from e
    return URLSafeTimedSerializer(signing_key, salt=salt)


def sign_cookie(value: str, signing_key: str, *, salt: str = "enlace-cookie") -> str:
    """Return a signed, URL-safe token carrying ``value``."""
    return _serializer(signing_key, salt=salt).dumps(value)


def verify_cookie(
    token: str,
    signing_key: str,
    *,
    max_age: Optional[int] = None,
    salt: str = "enlace-cookie",
) -> Optional[str]:
    """Return the original value iff the token is valid and unexpired."""
    try:
        from itsdangerous import BadSignature, SignatureExpired  # type: ignore
    except ImportError:
        return None
    ser = _serializer(signing_key, salt=salt)
    try:
        return ser.loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    except Exception:
        return None
