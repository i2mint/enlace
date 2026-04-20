"""CSRFMiddleware: double-submit accept/reject, exempt paths, cookie issuance."""

import asyncio

from enlace.auth import sign_cookie
from enlace.auth.middleware import CSRFMiddleware

SIGNING_KEY = "csrf-signing-key-32bytes-minimumlen"


class _Cap:
    def __init__(self):
        self.messages: list[dict] = []

    async def send(self, msg):
        self.messages.append(msg)

    async def receive(self):
        return {"type": "http.request", "body": b"", "more_body": False}

    def status(self) -> int:
        for m in self.messages:
            if m["type"] == "http.response.start":
                return m["status"]
        return -1

    def set_cookie_headers(self) -> list[bytes]:
        out: list[bytes] = []
        for m in self.messages:
            if m["type"] == "http.response.start":
                for k, v in m.get("headers", []):
                    if k.lower() == b"set-cookie":
                        out.append(v)
        return out


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _scope(method, path, cookies=None, headers=None):
    hdrs = list(headers or [])
    if cookies:
        hdrs.append(
            (b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode())
        )
    return {"type": "http", "method": method, "path": path, "headers": hdrs}


def test_safe_method_sets_cookie_when_missing():
    mw = CSRFMiddleware(_ok, signing_key=SIGNING_KEY)
    cap = _Cap()
    asyncio.run(mw(_scope("GET", "/"), cap.receive, cap.send))
    assert cap.status() == 200
    headers = cap.set_cookie_headers()
    assert any(b"enlace_csrf=" in h for h in headers)


def test_state_changing_without_cookie_rejected():
    mw = CSRFMiddleware(_ok, signing_key=SIGNING_KEY)
    cap = _Cap()
    asyncio.run(mw(_scope("POST", "/x"), cap.receive, cap.send))
    assert cap.status() == 403


def test_state_changing_with_matching_header_accepted():
    raw_token = "abc123"
    signed = sign_cookie(raw_token, SIGNING_KEY, salt="csrf")
    mw = CSRFMiddleware(_ok, signing_key=SIGNING_KEY)
    cap = _Cap()
    asyncio.run(
        mw(
            _scope(
                "POST",
                "/x",
                cookies={"enlace_csrf": signed},
                headers=[(b"x-csrf-token", raw_token.encode())],
            ),
            cap.receive,
            cap.send,
        )
    )
    assert cap.status() == 200


def test_state_changing_with_mismatched_header_rejected():
    signed = sign_cookie("abc", SIGNING_KEY, salt="csrf")
    mw = CSRFMiddleware(_ok, signing_key=SIGNING_KEY)
    cap = _Cap()
    asyncio.run(
        mw(
            _scope(
                "POST",
                "/x",
                cookies={"enlace_csrf": signed},
                headers=[(b"x-csrf-token", b"wrong")],
            ),
            cap.receive,
            cap.send,
        )
    )
    assert cap.status() == 403


def test_exempt_path_skips_check():
    mw = CSRFMiddleware(_ok, signing_key=SIGNING_KEY)
    cap = _Cap()
    asyncio.run(mw(_scope("POST", "/auth/callback/google"), cap.receive, cap.send))
    assert cap.status() == 200
