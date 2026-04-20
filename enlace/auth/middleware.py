"""Platform auth middleware (pure ASGI).

Runs before any mounted sub-app. Responsibilities:

1. Normalize the request path and reject known traversal bypasses.
2. Strip client-provided identity headers so apps can't be fooled by spoofed
   ``X-User-ID`` / ``X-Forwarded-User`` / similar.
3. Resolve the access level for the request by longest-prefix match on the
   route prefix of each mounted app. Deny-by-default: an unmatched path is
   treated as ``protected:user``.
4. For ``public`` / ``local``, pass through with ``user_id=None``.
5. For ``protected:shared``, look for a per-app signed cookie; on failure
   return 401.
6. For ``protected:user``, look for the platform session cookie, load the
   session from ``SessionStore``, set ``user_id`` / ``user_email``; on
   failure return 401.

Design notes:
- Pure ASGI three-callable pattern. Never ``BaseHTTPMiddleware`` (see
  CLAUDE.md).
- Exempts ``/auth/*`` from auth checks so login/register pages stay reachable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import unquote

from enlace.auth.cookies import verify_cookie
from enlace.auth.sessions import SessionStore

# Headers clients could try to spoof identity with. Always stripped inbound.
_IDENTITY_HEADERS = (
    b"x-user-id",
    b"x-user-email",
    b"x-forwarded-user",
    b"x-forwarded-email",
    b"x-remote-user",
    b"x-remote-email",
    b"x-auth-user",
)

# Traversal markers that should never appear in a normalized path. We reject
# both raw and percent-encoded forms without touching the rest of the path.
_TRAVERSAL_MARKERS = ("..", "\\", "\x00")
_ENCODED_TRAVERSAL = ("%2e%2e", "%2f%2f", "%5c", "%00")


@dataclass(frozen=True)
class AccessRule:
    """Auth policy for a single mount prefix."""

    prefix: str  # e.g. "/api/my_app" or "/my_app"
    level: str  # "public" | "protected:shared" | "protected:user" | "local"
    app_id: str
    shared_password_hash: Optional[str] = None


def _normalize_path(raw_path: str) -> Optional[str]:
    """Return a normalized path or None if the path contains a traversal marker.

    "Normalized" here means collapsed ``//`` and decoded once. We don't try to
    canonicalize `.` vs `./` or case; the goal is to reject known attacks, not
    to rewrite every input.
    """
    if not raw_path:
        return "/"
    lowered = raw_path.lower()
    for m in _ENCODED_TRAVERSAL:
        if m in lowered:
            return None
    decoded = unquote(raw_path)
    for m in _TRAVERSAL_MARKERS:
        if m in decoded:
            return None
    while "//" in decoded:
        decoded = decoded.replace("//", "/")
    return decoded


def _strip_identity_headers(
    headers: list[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    return [(k, v) for (k, v) in headers if k.lower() not in _IDENTITY_HEADERS]


def _parse_cookies(header_value: bytes) -> dict[str, str]:
    """Light RFC-6265 cookie parser that only returns the first occurrence per name."""
    out: dict[str, str] = {}
    for part in header_value.decode("latin-1").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name not in out:
            out[name] = value.strip()
    return out


def _get_cookies(scope) -> dict[str, str]:
    for k, v in scope.get("headers", []):
        if k.lower() == b"cookie":
            return _parse_cookies(v)
    return {}


def _longest_prefix(path: str, rules: Iterable[AccessRule]) -> Optional[AccessRule]:
    best: Optional[AccessRule] = None
    best_len = -1
    for r in rules:
        if path == r.prefix or path.startswith(r.prefix.rstrip("/") + "/"):
            if len(r.prefix) > best_len:
                best = r
                best_len = len(r.prefix)
    return best


async def _send_json_response(send, status: int, body: dict):
    import json as _json

    data = _json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": data})


async def _reject_websocket(send):
    await send({"type": "websocket.close", "code": 1008})


class PlatformAuthMiddleware:
    """Pure-ASGI auth middleware. See module docstring for behavior."""

    def __init__(
        self,
        app,
        *,
        access_rules: Iterable[AccessRule],
        session_store: SessionStore,
        signing_key: str,
        cookie_name: str = "enlace_session",
        max_age: int = 86400,
        auth_path_prefix: str = "/auth",
    ):
        self.app = app
        self._rules = list(access_rules)
        self._sessions = session_store
        self._signing_key = signing_key
        self._cookie = cookie_name
        self._max_age = max_age
        self._auth_prefix = auth_path_prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        raw_path = scope.get("path", "/")
        normalized = _normalize_path(raw_path)
        if normalized is None:
            if scope["type"] == "websocket":
                await _reject_websocket(send)
            else:
                await _send_json_response(send, 400, {"detail": "Bad request path"})
            return

        scope["headers"] = _strip_identity_headers(scope.get("headers", []))
        state = scope.setdefault("state", {})
        state.setdefault("user_id", None)
        state.setdefault("user_email", None)

        # /auth/* endpoints bypass auth so login / register / logout stay reachable.
        if (
            normalized.startswith(self._auth_prefix + "/")
            or normalized == self._auth_prefix
        ):
            await self.app(scope, receive, send)
            return

        # Resolve auth rule for this path.
        rule = _longest_prefix(normalized, self._rules)
        level = rule.level if rule is not None else "protected:user"
        if rule is not None:
            state["app_id"] = rule.app_id

        cookies = _get_cookies(scope)

        if level in ("public", "local"):
            # Public paths still opportunistically populate user_id if the
            # session cookie is valid, so downstream handlers (e.g. /_apps)
            # can filter by access level for authenticated users.
            token = cookies.get(self._cookie)
            if token:
                session_id = verify_cookie(
                    token, self._signing_key, max_age=self._max_age, salt="session"
                )
                session = self._sessions.get(session_id) if session_id else None
                if session is not None:
                    state["user_id"] = session.get("user_id")
                    state["user_email"] = session.get("email")
            await self.app(scope, receive, send)
            return

        if level == "protected:shared":
            app_id = rule.app_id if rule is not None else ""
            name = f"shared_auth_{app_id}"
            token = cookies.get(name)
            if (
                not token
                or verify_cookie(
                    token,
                    self._signing_key,
                    max_age=self._max_age,
                    salt=f"shared:{app_id}",
                )
                is None
            ):
                return await self._deny(scope, send, "shared")
            state["user_id"] = "shared"
            await self.app(scope, receive, send)
            return

        if level == "protected:user":
            token = cookies.get(self._cookie)
            session_id = None
            if token:
                session_id = verify_cookie(
                    token, self._signing_key, max_age=self._max_age, salt="session"
                )
            session = self._sessions.get(session_id) if session_id else None
            if session is None:
                return await self._deny(scope, send, "user")
            state["user_id"] = session.get("user_id")
            state["user_email"] = session.get("email")
            await self.app(scope, receive, send)
            return

        # Unknown level — deny by default.
        await self._deny(scope, send, "unknown")

    async def _deny(self, scope, send, kind: str):
        if scope["type"] == "websocket":
            await _reject_websocket(send)
            return
        await _send_json_response(
            send, 401, {"detail": "Not authenticated", "auth": kind}
        )


# ---------------------------------------------------------------------------
# CSRF middleware
# ---------------------------------------------------------------------------


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class CSRFMiddleware:
    """Signed double-submit CSRF for state-changing requests.

    On safe-method requests, sets an ``enlace_csrf`` cookie if one isn't
    present. On state-changing requests, requires the cookie and an
    ``X-CSRF-Token`` header to match after signature verification.

    Exempt paths skip the check entirely — typically OAuth callbacks and
    the shared-login endpoint, which receive state-changing requests from
    cross-origin redirects.
    """

    def __init__(
        self,
        app,
        *,
        signing_key: str,
        cookie_name: str = "enlace_csrf",
        header_name: str = "X-CSRF-Token",
        exempt_prefixes: Iterable[str] = ("/auth/callback", "/auth/login/"),
    ):
        self.app = app
        self._signing_key = signing_key
        self._cookie = cookie_name
        self._header = header_name.lower().encode("latin-1")
        self._exempt = tuple(exempt_prefixes)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")

        # Exempt paths skip the check entirely.
        is_exempt = any(path.startswith(p) for p in self._exempt)

        cookies = _get_cookies(scope)
        existing = cookies.get(self._cookie)
        existing_value = (
            verify_cookie(existing, self._signing_key, salt="csrf")
            if existing
            else None
        )

        if method in _SAFE_METHODS or is_exempt:
            # Ensure a token is present on safe requests.
            if existing_value is None:
                await self._send_with_csrf_cookie(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        # State-changing request — enforce double-submit.
        header_value = None
        for k, v in scope.get("headers", []):
            if k.lower() == self._header:
                header_value = v.decode("latin-1")
                break

        if (
            existing_value is None
            or header_value is None
            or header_value != existing_value
        ):
            await _send_json_response(send, 403, {"detail": "CSRF check failed"})
            return

        await self.app(scope, receive, send)

    async def _send_with_csrf_cookie(self, scope, receive, send):
        """Wrap the downstream response to inject a Set-Cookie for CSRF."""
        import secrets

        from enlace.auth.cookies import sign_cookie

        new_value = secrets.token_urlsafe(32)
        signed = sign_cookie(new_value, self._signing_key, salt="csrf")
        cookie_header = (f"{self._cookie}={signed}; Path=/; SameSite=Lax").encode(
            "latin-1"
        )

        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                headers = list(message.get("headers", []))
                headers.append((b"set-cookie", cookie_header))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped_send)
