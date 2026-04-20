"""PrefixedStore — MutableMapping wrapper that scopes keys under a prefix.

The per-user injection pattern: a single base store is shared across all users
and apps, but each request sees a ``PrefixedStore(base, f"{user_id}/{app_id}/")``
so keys can't collide across tenants.

Keys the caller passes are validated via ``sanitize_key``; the prefix itself is
sanitized at construction (each slash-separated segment).
"""

from collections.abc import Iterator, MutableMapping
from typing import Any

from enlace.stores.validation import sanitize_key


def _validate_prefix(prefix: str) -> str:
    """Sanitize each slash-separated segment of the prefix."""
    if not prefix:
        raise ValueError("prefix must be non-empty")
    if not prefix.endswith("/"):
        prefix = prefix + "/"
    parts = [p for p in prefix.split("/") if p]
    if not parts:
        raise ValueError("prefix must contain at least one segment")
    for p in parts:
        sanitize_key(p)
    return "/".join(parts) + "/"


class PrefixedStore(MutableMapping):
    """Transparently prepend a prefix to every key operation on a base store."""

    def __init__(self, base: MutableMapping, prefix: str):
        self._base = base
        self._prefix = _validate_prefix(prefix)

    @property
    def prefix(self) -> str:
        return self._prefix

    def _k(self, key: str) -> str:
        return self._prefix + sanitize_key(key)

    def __getitem__(self, key: str) -> Any:
        return self._base[self._k(key)]

    def __setitem__(self, key: str, value: Any) -> None:
        self._base[self._k(key)] = value

    def __delitem__(self, key: str) -> None:
        del self._base[self._k(key)]

    def __iter__(self) -> Iterator[str]:
        p = self._prefix
        plen = len(p)
        for k in self._base:
            if isinstance(k, str) and k.startswith(p):
                yield k[plen:]

    def __len__(self) -> int:
        return sum(1 for _ in iter(self))

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        try:
            return self._k(key) in self._base
        except ValueError:
            return False

    def __repr__(self) -> str:
        return f"PrefixedStore(prefix={self._prefix!r})"
