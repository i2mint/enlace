"""An external upstream never sees, nor sets, the platform's own cookies.

``mode="external"`` proxies to a server outside the platform. Forwarding the
visitor's ``Cookie`` header verbatim would hand it their platform session; an
upstream ``Set-Cookie`` for a platform cookie name would overwrite it on the
platform's origin.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from enlace.base import AppConfig, PlatformConfig
from enlace.proxy import _HttpxProxy, make_proxy_app, platform_cookie_filter
from enlace.strategies import ExternalStrategy


def _echo_transport(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            headers=[
                ("set-cookie", "enlace_session=forged; Path=/"),
                ("set-cookie", "shared_auth_vault=forged; Path=/"),
                ("set-cookie", "space_pref=dark; Path=/typola"),
            ],
            json={"ok": True},
        )

    return httpx.MockTransport(handler)


def _client(proxy: _HttpxProxy, seen: dict) -> TestClient:
    async def _get_client():
        return httpx.AsyncClient(transport=_echo_transport(seen))

    proxy._get_client = _get_client
    return TestClient(Starlette(routes=[Mount("/typola", app=proxy)]))


VISITOR_COOKIES = (
    "enlace_session=SECRET-SESSION; enlace_csrf=SECRET-CSRF; "
    "shared_auth_vault=SECRET-SHARED; space_pref=light"
)


def test_filtered_proxy_forwards_only_non_platform_cookies():
    seen: dict = {}
    proxy = make_proxy_app(
        upstream="https://space.example",
        strip_prefix="/typola",
        cookie_filter=platform_cookie_filter(),
    )
    r = _client(proxy, seen).get("/typola/x", headers={"Cookie": VISITOR_COOKIES})
    assert r.status_code == 200
    assert seen["cookie"] == "space_pref=light"
    assert "SECRET" not in (seen["cookie"] or "")
    set_cookies = r.headers.get_list("set-cookie")
    assert set_cookies == ["space_pref=dark; Path=/typola"]


def test_filtered_proxy_drops_cookie_header_when_nothing_is_left():
    seen: dict = {}
    proxy = make_proxy_app(
        upstream="https://space.example",
        strip_prefix="/typola",
        cookie_filter=platform_cookie_filter(),
    )
    _client(proxy, seen).get("/typola/x", headers={"Cookie": "enlace_session=SECRET"})
    assert seen["cookie"] is None


def test_unfiltered_proxy_is_unchanged():
    """Process-mode (local, part of the platform) keeps forwarding everything."""
    seen: dict = {}
    proxy = make_proxy_app(upstream="http://127.0.0.1:9", strip_prefix="/typola")
    r = _client(proxy, seen).get("/typola/x", headers={"Cookie": VISITOR_COOKIES})
    assert seen["cookie"] == VISITOR_COOKIES
    assert len(r.headers.get_list("set-cookie")) == 3


@pytest.mark.parametrize(
    "auth, extra_name",
    [({}, None), ({"session_cookie_name": "my_sess"}, "my_sess")],
)
def test_external_strategy_applies_the_platform_filter(tmp_path, auth, extra_name):
    platform = PlatformConfig(apps_dir=tmp_path, auth=auth)
    app = AppConfig(
        name="typola",
        route_prefix="/typola",
        app_type="asgi_app",
        mode="external",
        upstream_url="https://space.example",
    )
    proxy = ExternalStrategy().make_asgi(app, platform)
    keep = proxy.cookie_filter
    assert keep is not None
    assert not keep("enlace_session")
    assert not keep("enlace_csrf")
    assert not keep("shared_auth_anything")
    assert keep("space_pref")
    if extra_name:
        assert not keep(extra_name)
