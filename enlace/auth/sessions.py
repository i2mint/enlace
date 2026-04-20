"""Session storage backed by a MutableMapping.

A session is ``{"user_id": str, "email": str | None, "created_at": float}``.
Session IDs are 32-byte urlsafe tokens. Revocation is a simple delete.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import MutableMapping
from typing import Any, Optional


class SessionStore:
    """Thin adapter around a MutableMapping that speaks session semantics."""

    def __init__(self, store: MutableMapping):
        self._store = store

    def create(self, user_id: str, email: Optional[str] = None) -> str:
        session_id = secrets.token_urlsafe(32)
        self._store[session_id] = {
            "user_id": user_id,
            "email": email,
            "created_at": time.time(),
        }
        return session_id

    def get(self, session_id: str) -> Optional[dict[str, Any]]:
        try:
            value = self._store[session_id]
        except KeyError:
            return None
        if not isinstance(value, dict):
            return None
        return value

    def delete(self, session_id: str) -> bool:
        try:
            del self._store[session_id]
            return True
        except KeyError:
            return False

    def list_all(self) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        for sid in list(self._store):
            try:
                value = self._store[sid]
            except KeyError:
                continue
            if isinstance(value, dict):
                out.append((sid, value))
        return out
