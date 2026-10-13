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
from enlace.strategies import ExternalStrategy, ProcessStrategy


def _echo_transport(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        seen["authorization"] = request.headers.get("authorization")
        seen["x-csrf-token"] = request.headers.get("x-csrf-token")
        return httpx.Response(
            200,
            headers=[
                ("set-cookie", "enlace_session=forged; Path=/"),
                ("set-cookie", "shared_auth_vault=forged; Path=/"),
                ("set-cookie", "space_pref=dark; Path=/typola"),
                ("set-cookie", "=enlace_session=forged; Path=/auth"),
                ("set-cookie", " =enlace_csrf=forged"),
                ("set-cookie", "nameless-value; Path=/"),
                ("clear-site-data", '"cookies"'),
                ("service-worker-allowed", "/"),
                ("x-upstream", "kept"),
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
    """``make_proxy_app`` with no filter forwards everything (strategies opt in)."""
    seen: dict = {}
    proxy = make_proxy_app(upstream="http://127.0.0.1:9", strip_prefix="/typola")
    r = _client(proxy, seen).get("/typola/x", headers={"Cookie": VISITOR_COOKIES})
    assert seen["cookie"] == VISITOR_COOKIES
    assert len(r.headers.get_list("set-cookie")) == 6
    assert r.headers.get("clear-site-data") == '"cookies"'


def test_external_strategy_isolates_upstream(tmp_path):
    """Through ExternalStrategy: no platform credentials out, no takeover in."""
    seen: dict = {}
    app = AppConfig(
        name="typola",
        route_prefix="/typola",
        app_type="asgi_app",
        mode="external",
        upstream_url="https://space.example",
    )
    proxy = ExternalStrategy().make_asgi(app, PlatformConfig(apps_dir=tmp_path))
    r = _client(proxy, seen).get(
        "/typola/x",
        headers={
            "Cookie": VISITOR_COOKIES,
            "X-CSRF-Token": "SECRET-CSRF",
            "Authorization": "Bearer upstream-own-token",
        },
    )
    assert seen["cookie"] == "space_pref=light"
    assert seen["x-csrf-token"] is None
    assert seen["authorization"] == "Bearer upstream-own-token"
    assert r.headers.get_list("set-cookie") == ["space_pref=dark; Path=/typola"]
    assert "clear-site-data" not in r.headers
    assert "service-worker-allowed" not in r.headers
    assert r.headers["x-upstream"] == "kept"


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


# --- mode="process" (i2mint/enlace#60) ---------------------------------------
# A local process is still a separate program with its own dependency set, and
# a bearer-token app (an MCP connector) never needs the platform session.


def _process_app(**overrides) -> AppConfig:
    fields = dict(
        name="connector",
        route_prefix="/typola",  # the mount _client uses
        app_type="asgi_app",
        mode="process",
        command=["serve"],
        port=9,
    )
    return AppConfig(**{**fields, **overrides})


def _through_process(tmp_path, app, seen):
    proxy = ProcessStrategy().make_asgi(app, PlatformConfig(apps_dir=tmp_path))
    return _client(proxy, seen).get(
        "/typola/x",
        headers={
            "Cookie": VISITOR_COOKIES,
            "X-CSRF-Token": "SECRET-CSRF",
            "Authorization": "Bearer connector-own-token",
        },
    )


def test_process_strategy_withholds_platform_credentials_by_default(tmp_path):
    seen: dict = {}
    r = _through_process(tmp_path, _process_app(), seen)
    assert seen["cookie"] == "space_pref=light"
    assert seen["x-csrf-token"] is None
    assert seen["authorization"] == "Bearer connector-own-token"
    assert r.headers.get_list("set-cookie") == ["space_pref=dark; Path=/typola"]
    assert "clear-site-data" not in r.headers
    assert "service-worker-allowed" not in r.headers
    assert r.headers["x-upstream"] == "kept"


def test_process_strategy_opt_in_forwards_credentials_but_never_lets_them_be_set(
    tmp_path,
):
    """``forward_platform_credentials`` is a read grant, not a write grant."""
    seen: dict = {}
    app = _process_app(forward_platform_credentials=True)
    r = _through_process(tmp_path, app, seen)
    assert seen["cookie"] == VISITOR_COOKIES
    assert seen["x-csrf-token"] == "SECRET-CSRF"
    assert r.headers.get_list("set-cookie") == ["space_pref=dark; Path=/typola"]
    assert "clear-site-data" not in r.headers
    assert "service-worker-allowed" not in r.headers


def test_process_strategy_honours_a_custom_session_cookie_name(tmp_path):
    platform = PlatformConfig(
        apps_dir=tmp_path, auth={"session_cookie_name": "my_sess"}
    )
    proxy = ProcessStrategy().make_asgi(_process_app(), platform)
    assert not proxy.cookie_filter("my_sess")
    assert not proxy.set_cookie_filter("my_sess")
    assert proxy.cookie_filter("space_pref")


def test_forward_platform_credentials_is_an_app_toml_key():
    from enlace.discover import _resolved_field_map

    field_map, _ = _resolved_field_map()
    assert field_map["forward_platform_credentials"] == "forward_platform_credentials"
    assert _process_app().forward_platform_credentials is False


def test_split_cookie_headers_all_survive_when_credentials_are_forwarded(tmp_path):
    """HTTP/2 sends one Cookie field per cookie; none may be lost (#60 review)."""
    seen: dict = {}
    proxy = ProcessStrategy().make_asgi(
        _process_app(forward_platform_credentials=True),
        PlatformConfig(apps_dir=tmp_path),
    )
    _client(proxy, seen).get(
        "/typola/x", headers=[("cookie", "enlace_session=S"), ("cookie", "b=2")]
    )
    assert seen["cookie"] == "enlace_session=S; b=2"


def test_split_cookie_headers_are_each_filtered(tmp_path):
    seen: dict = {}
    proxy = ProcessStrategy().make_asgi(
        _process_app(), PlatformConfig(apps_dir=tmp_path)
    )
    _client(proxy, seen).get(
        "/typola/x",
        headers=[("cookie", "a=1"), ("cookie", "enlace_session=S"), ("cookie", "b=2")],
    )
    assert seen["cookie"] == "a=1; b=2"


def test_the_social_login_state_cookie_is_a_platform_cookie():
    """A proxied app setting a second ``enlace_oauth_state`` would block sign-in."""
    keep = platform_cookie_filter()
    assert not keep("enlace_oauth_state")


def _proxy_answering(headers: list, seen: dict) -> TestClient:
    """A filtered proxy whose upstream answers with *headers*."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        return httpx.Response(302, headers=headers)

    proxy = make_proxy_app(
        upstream="https://space.example",
        strip_prefix="/typola",
        cookie_filter=platform_cookie_filter(),
    )

    async def _get_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    proxy._get_client = _get_client
    return TestClient(Starlette(routes=[Mount("/typola", app=proxy)]))


def test_case_variant_platform_cookie_names_are_platform_cookies():
    """#66: ``Enlace_Session`` may not be set by, or sent to, an upstream."""
    seen: dict = {}
    client = _proxy_answering(
        [
            ("set-cookie", "Enlace_Session=forged; Path=/"),
            ("set-cookie", "ENLACE_CSRF=forged"),
            ("set-cookie", "Shared_Auth_vault=forged"),
            ("set-cookie", "space_pref=dark"),
        ],
        seen,
    )
    r = client.get(
        "/typola/x",
        headers={"Cookie": "ENLACE_SESSION=S; space_pref=light"},
        follow_redirects=False,
    )
    assert r.headers.get_list("set-cookie") == ["space_pref=dark"]
    assert seen["cookie"] == "space_pref=light"


def test_custom_platform_cookie_names_match_case_insensitively():
    keep = platform_cookie_filter(names=["My_Sess"], prefixes=["Vault_"])
    assert not keep("my_sess") and not keep("MY_SESS") and not keep("vault_x")
    assert keep("my_sess2") and keep("theme")


def test_upstream_redirects_pass_through_unfiltered():
    """#66 decision: Location/Refresh are navigation, not a credential leak.

    Any cross-site page can already navigate a visitor to /auth/..., and a
    proxied app's pages run on the platform origin; GET routes must be safe on
    their own. See the enlace.proxy module docstring.
    """
    client = _proxy_answering(
        [("location", "/auth/logout"), ("refresh", "0; url=/auth/logout")], {}
    )
    r = client.get("/typola/x", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/logout"
    assert r.headers["refresh"] == "0; url=/auth/logout"
