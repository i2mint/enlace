"""Key sanitization for user-supplied store keys.

Blocks the standard path-traversal attack surface: ``..`` segments, backslashes,
null bytes, control characters, absolute paths, and URL-encoded variants. Keys
that survive sanitization are still filesystem-safe and can be composed into a
prefix without risk of escaping the tenant namespace.
"""

from urllib.parse import unquote

_BAD_SEGMENTS = ("..", "\\", "\x00")

# URL-encoded forms of ``..``, ``/``, ``\``, ``%`` that attackers use to slip
# traversal past naive validators.
_ENCODED_MARKERS = ("%2e", "%2f", "%5c", "%00", "%25")


def sanitize_key(key: str) -> str:
    """Return ``key`` unchanged if safe for use as a store path component.

    Raises ``ValueError`` with a specific reason if the key is unsafe. The goal
    is fail-fast: we want the caller to see exactly why a key was rejected, not
    a silently rewritten value.
    """
    if not isinstance(key, str):
        raise ValueError(f"key must be a string, got {type(key).__name__}")
    if not key:
        raise ValueError("key must be non-empty")
    if len(key) > 1024:
        raise ValueError("key is too long (max 1024 chars)")

    lowered = key.lower()
    for marker in _ENCODED_MARKERS:
        if marker in lowered:
            raise ValueError(
                f"key contains URL-encoded marker '{marker}' — "
                "decode and sanitize before passing in"
            )

    # Decode once as a defence in depth; if the decoded form differs, the
    # caller was trying something clever. Compare case-insensitively so the
    # check doesn't reject plain percent-less keys that happened to round-trip.
    decoded = unquote(key)
    if decoded != key:
        raise ValueError("key contains percent-encoding")

    for seg in _BAD_SEGMENTS:
        if seg in key:
            raise ValueError(f"key contains disallowed substring '{seg!r}'")

    if key.startswith("/") or key.startswith("."):
        raise ValueError("key must not start with '/' or '.'")

    for ch in key:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise ValueError(f"key contains control character U+{code:04X}")

    return key
