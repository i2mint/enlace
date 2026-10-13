"""The proxy forwards the path as the client encoded it (enlace#74).

ASGI servers decode ``scope["path"]``; building the upstream URL from it turned
``a%2Fb`` into ``a/b``, moved ``%3F`` into the query, and sent a bare ``%``.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from enlace.proxy import _upstream_path, make_proxy_app

PREFIX = "/api/blog"


def _uvicorn_scope(raw: str) -> dict:
    """The scope uvicorn builds: raw bytes as sent, path decoded (errors replaced)."""
    from urllib.parse import unquote

    return {"path": unquote(raw), "raw_path": raw.encode("latin-1")}


@pytest.mark.parametrize(
    "raw, upstream",
    [
        ("/api/blog/a%2Fb", "/a%2Fb"),
        ("/api/blog/a%3Fb", "/a%3Fb"),
        ("/api/blog/100%25", "/100%25"),
        ("/api/blog/caf%C3%A9", "/caf%C3%A9"),
        ("/api/blog/a%20b/c", "/a%20b/c"),
        ("/api/blog/%FF", "/%EF%BF%BD"),  # not UTF-8: re-quoted, as decoded
        ("/api/blog/a//b", "/a//b"),
        ("/api/blog/a@b:c", "/a@b:c"),  # pchar, raw branch, authority unchanged
        ("/api/blog/a%23b", "/a%23b"),
        ("/api/blog", "/"),
        ("/api/blog/", "/"),
        ("/api/%62log/x%2Fy", "/x%2Fy"),  # an encoded prefix still lines up
    ],
)
def test_raw_path_is_forwarded(raw, upstream):
    assert _upstream_path(_uvicorn_scope(raw), PREFIX) == upstream


@pytest.mark.parametrize(
    "scope, upstream",
    [
        ({"path": "/api/blog/a/b"}, "/a/b"),  # no raw_path: re-quote the decoded one
        ({"path": "/api/blog/100%"}, "/100%25"),
        ({"path": "/api/blog/a?b#c"}, "/a%3Fb%23c"),
        ({"path": "/api/blog/café"}, "/caf%C3%A9"),
        # raw_path that does not decode to path is not trusted
        ({"path": "/api/blog/a/b", "raw_path": b"/api/blog/other"}, "/a/b"),
        # a raw prefix that is not the mount's
        ({"path": "/api/blog/x", "raw_path": b"/api/other/x"}, "/x"),
        # not well-formed on the wire
        ({"path": "/api/blog/a b", "raw_path": b"/api/blog/a b"}, "/a%20b"),
    ],
)
def test_untrusted_or_missing_raw_path_falls_back_to_quoting(scope, upstream):
    assert _upstream_path(scope, PREFIX) == upstream


@pytest.mark.parametrize(
    "raw",
    [
        "/api/blog/%2E%2E/admin",
        "/api/blog/a/../b",
        "/api/blog/a%2F..%2Fb",
        "/api/blog/./x",
    ],
)
def test_dot_segments_are_not_forwarded_escaped(raw):
    """httpx resolves literal dot segments, so an escaped ``..`` must not slip past."""
    out = _upstream_path(_uvicorn_scope(raw), PREFIX)
    assert "%2E" not in out.upper() and "%2F" not in out.upper()
    resolved = httpx.URL("http://127.0.0.1:9100" + out)
    assert resolved.host == "127.0.0.1" and resolved.port == 9100
    assert ".." not in resolved.raw_path.decode().split("/")


@pytest.mark.parametrize(
    "path",
    ["/api/blog@evil.example/x", "/api/blogx", "@evil.example", "", "//evil.example/x"],
)
def test_the_upstream_authority_cannot_change(path):
    out = _upstream_path({"path": path}, PREFIX)
    assert out.startswith("/")
    url = httpx.URL("http://127.0.0.1:9100" + out)
    assert (url.host, url.port) == ("127.0.0.1", 9100)


def test_through_a_mount_end_to_end():
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.raw_path, request.url.query))
        return httpx.Response(200)

    proxy = make_proxy_app(upstream="http://127.0.0.1:9100", strip_prefix=PREFIX)

    async def _get_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    proxy._get_client = _get_client
    client = TestClient(Starlette(routes=[Mount(PREFIX, app=proxy)]))
    assert client.get("/api/blog/a%2Fb?q=1").status_code == 200
    assert client.get("/api/blog/100%25").status_code == 200
    assert client.get("/api/blog/a%3Fb").status_code == 200
    assert seen == [(b"/a%2Fb?q=1", b"q=1"), (b"/100%25", b""), (b"/a%3Fb", b"")]


@pytest.mark.parametrize("prefix", ["", "/", "/api/blog/"])
def test_degenerate_prefixes(prefix):
    scope = _uvicorn_scope("/api/blog/a%2Fb")
    out = _upstream_path(scope, prefix)
    assert out == ("/a%2Fb" if prefix == "/api/blog/" else "/api/blog/a%2Fb")


def test_a_raw_path_holding_a_query_is_not_trusted():
    """Some servers once put the query in raw_path; it must not reach the path."""
    scope = {"path": "/api/blog/a", "raw_path": b"/api/blog/a?x=1"}
    assert _upstream_path(scope, PREFIX) == "/a"


def test_a_lone_surrogate_does_not_500():
    assert _upstream_path({"path": "/api/blog/a\udc80"}, PREFIX).startswith("/a")


def test_an_overlong_dot_is_not_forwarded_verbatim():
    """``%C0%AE`` is an overlong dot; a lenient upstream could decode it as one."""
    out = _upstream_path(_uvicorn_scope("/api/blog/%C0%AE%C0%AE/x"), PREFIX)
    assert "%C0" not in out.upper() and out.endswith("/x")
