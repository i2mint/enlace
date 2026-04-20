"""PlatformAuthMiddleware: path normalization, header stripping, access rules."""

import asyncio

import pytest

from enlace.auth import SessionStore, sign_cookie
from enlace.auth.middleware import (
    AccessRule,
    PlatformAuthMiddleware,
    _normalize_path,
    _strip_identity_headers,
)

SIGNING_KEY = "test-signing-key-32bytes-minimumlen"


class _Capture:
    """Helper: run an ASGI app and capture the response messages."""

    def __init__(self):
        self.messages: list[dict] = []
        self.called_downstream = False

    async def send(self, msg):
        self.messages.append(msg)

    async def receive(self):
        return {"type": "http.request", "body": b"", "more_body": False}

    def status(self) -> int:
        for m in self.messages:
            if m["type"] == "http.response.start":
                return m["status"]
        return -1


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _run(coro):
    return (
        asyncio.get_event_loop().run_until_complete(coro)
        if False
        else asyncio.run(coro)
    )


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/foo", "/foo"),
        ("//foo//bar", "/foo/bar"),
        ("/foo/..", None),
        ("/foo/%2e%2e/bar", None),
        ("/foo/%2e%2e%2fbar", None),
        ("/foo/%2f%2fbar", None),
        ("/a\\b", None),
        ("/a%00b", None),
    ],
)
def test_path_normalization(path, expected):
    assert _normalize_path(path) == expected


def test_strip_identity_headers_removes_spoofed():
    headers = [
        (b"content-type", b"application/json"),
        (b"x-user-id", b"attacker"),
        (b"X-Forwarded-User", b"bob"),
        (b"accept", b"*/*"),
    ]
    out = _strip_identity_headers(headers)
    names = {k.lower() for k, _ in out}
    assert b"x-user-id" not in names
    assert b"x-forwarded-user" not in names
    assert b"content-type" in names


def _make_mw(rules=None, session_store=None):
    rules = rules or []
    session_store = session_store or SessionStore({})
    return PlatformAuthMiddleware(
        _ok_app,
        access_rules=rules,
        session_store=session_store,
        signing_key=SIGNING_KEY,
    )


def _http_scope(path, cookies=None):
    headers = []
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", cookie_str.encode()))
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers,
        "state": {},
    }


def test_public_app_passes_through():
    mw = _make_mw([AccessRule(prefix="/foo", level="public", app_id="foo")])
    cap = _Capture()
    _run(mw(_http_scope("/foo/stuff"), cap.receive, cap.send))
    assert cap.status() == 200


def test_unknown_path_denied_by_default():
    mw = _make_mw([])
    cap = _Capture()
    _run(mw(_http_scope("/totally-unknown"), cap.receive, cap.send))
    assert cap.status() == 401


def test_protected_user_without_session_denied():
    mw = _make_mw([AccessRule(prefix="/api/x", level="protected:user", app_id="x")])
    cap = _Capture()
    _run(mw(_http_scope("/api/x/thing"), cap.receive, cap.send))
    assert cap.status() == 401


def test_protected_user_with_valid_session_accepted():
    sessions = SessionStore({})
    sid = sessions.create("alice", "alice@x")
    token = sign_cookie(sid, SIGNING_KEY, salt="session")
    mw = _make_mw(
        [AccessRule(prefix="/api/x", level="protected:user", app_id="x")],
        session_store=sessions,
    )
    cap = _Capture()
    _run(
        mw(
            _http_scope("/api/x/thing", {"enlace_session": token}),
            cap.receive,
            cap.send,
        )
    )
    assert cap.status() == 200


def test_protected_user_with_tampered_cookie_denied():
    sessions = SessionStore({})
    sid = sessions.create("alice", "alice@x")
    bad_token = sign_cookie(sid, "different-key", salt="session")
    mw = _make_mw(
        [AccessRule(prefix="/api/x", level="protected:user", app_id="x")],
        session_store=sessions,
    )
    cap = _Capture()
    _run(
        mw(
            _http_scope("/api/x/thing", {"enlace_session": bad_token}),
            cap.receive,
            cap.send,
        )
    )
    assert cap.status() == 401


def test_auth_prefix_bypasses_check():
    mw = _make_mw([])  # empty rules: everything else is deny-by-default
    cap = _Capture()
    _run(mw(_http_scope("/auth/login"), cap.receive, cap.send))
    assert cap.status() == 200


def test_longest_prefix_wins():
    rules = [
        AccessRule(prefix="/api", level="protected:user", app_id="catchall"),
        AccessRule(prefix="/api/public", level="public", app_id="public_one"),
    ]
    mw = _make_mw(rules)
    cap = _Capture()
    _run(mw(_http_scope("/api/public/ok"), cap.receive, cap.send))
    assert cap.status() == 200


def test_protected_shared_with_valid_cookie():
    rule = AccessRule(prefix="/s", level="protected:shared", app_id="s")
    mw = _make_mw([rule])
    token = sign_cookie("1", SIGNING_KEY, salt="shared:s")
    cap = _Capture()
    _run(mw(_http_scope("/s/page", {"shared_auth_s": token}), cap.receive, cap.send))
    assert cap.status() == 200


def test_protected_shared_without_cookie_denied():
    mw = _make_mw([AccessRule(prefix="/s", level="protected:shared", app_id="s")])
    cap = _Capture()
    _run(mw(_http_scope("/s/page"), cap.receive, cap.send))
    assert cap.status() == 401


def test_traversal_path_rejected():
    mw = _make_mw([AccessRule(prefix="/foo", level="public", app_id="foo")])
    cap = _Capture()
    _run(mw(_http_scope("/foo/%2e%2e/etc"), cap.receive, cap.send))
    assert cap.status() == 400
