"""Per-app response headers and cross-origin isolation (i2mint/enlace#113).

The acceptance: a sub-path an app lists in app.toml is served with the headers;
every other path of that app, and every other app, is unchanged.
"""

import textwrap

import pytest
from starlette.testclient import TestClient

from enlace import build_backend, discover_apps
from enlace.base import AppConfig, PlatformConfig
from enlace.response_headers import (
    CROSS_ORIGIN_ISOLATION_HEADERS,
    ResponseHeadersMiddleware,
    add_response_headers,
    header_rules_for_app,
)

COI = {k.lower(): v for k, v in CROSS_ORIGIN_ISOLATION_HEADERS.items()}

SERVER = textwrap.dedent("""\
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.get("/lab/models")
    def models():
        return {"models": []}

    @app.get("/other")
    def other():
        return {"ok": True}

    @app.get("/sets-its-own")
    def sets_its_own():
        return JSONResponse({}, headers={"Cross-Origin-Opener-Policy": "unsafe-none"})
""")


def _app(apps_dir, name, toml=""):
    d = apps_dir / name
    (d / "frontend" / "lab").mkdir(parents=True)
    (d / "frontend" / "labels").mkdir()
    (d / "frontend" / "index.html").write_text("<!doctype html><title>home</title>")
    (d / "frontend" / "lab" / "index.html").write_text(
        "<!doctype html><title>l</title>"
    )
    (d / "frontend" / "lab" / "worker.js").write_text("self.onmessage = () => {}")
    (d / "frontend" / "lab" / "tiny.onnx").write_bytes(b"\x08\x07" * 1024)
    (d / "frontend" / "labels" / "index.html").write_text("<title>x</title>")
    (d / "server.py").write_text(SERVER)
    (d / "app.toml").write_text(textwrap.dedent(toml))
    return d


@pytest.fixture
def client(tmp_path):
    apps = tmp_path / "apps"
    apps.mkdir()
    _app(apps, "iso", 'cross_origin_isolated = ["lab"]\n')
    _app(apps, "plain")
    config = discover_apps(PlatformConfig(apps_dir=apps))
    return TestClient(build_backend(config))


def _coi(response) -> dict[str, str]:
    return {k: response.headers[k] for k in COI if k in response.headers}


@pytest.mark.parametrize(
    "path",
    ["/iso/lab/", "/iso/lab/index.html", "/iso/lab/worker.js", "/api/iso/lab/models"],
)
def test_listed_subpath_is_isolated(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert _coi(r) == COI


@pytest.mark.parametrize(
    "path",
    [
        "/iso/",  # the rest of the same app's page
        "/iso/labels/",  # a sibling whose name starts with the sub-path
        "/api/iso/other",  # the rest of its API
        "/plain/",  # another app entirely
        "/plain/lab/",
        "/api/plain/lab/models",
        "/_apps",  # the platform itself
    ],
)
def test_everything_else_is_unchanged(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert _coi(r) == {}


def test_no_declaration_installs_no_middleware(tmp_path):
    apps = tmp_path / "apps"
    apps.mkdir()
    _app(apps, "plain")
    config = discover_apps(PlatformConfig(apps_dir=apps))
    stack = build_backend(config).user_middleware
    assert not any(m.cls is ResponseHeadersMiddleware for m in stack)


def test_whole_app_and_override_from_response_headers(tmp_path):
    apps = tmp_path / "apps"
    apps.mkdir()
    _app(
        apps,
        "iso",
        """\
        cross_origin_isolated = true

        [response_headers]
        "/" = { "X-Robots-Tag" = "noindex" }
        "lab" = { "cross-origin-embedder-policy" = "credentialless" }
        """,
    )
    c = TestClient(build_backend(discover_apps(PlatformConfig(apps_dir=apps))))
    home = c.get("/iso/")
    assert _coi(home) == COI
    assert home.headers["x-robots-tag"] == "noindex"
    lab = c.get("/iso/lab/")
    assert lab.headers["cross-origin-embedder-policy"] == "credentialless"
    assert lab.headers["cross-origin-opener-policy"] == "same-origin"  # kept
    assert lab.headers["x-robots-tag"] == "noindex"  # inherited from "/"
    assert lab.headers.get_list("cross-origin-embedder-policy") == ["credentialless"]
    # a declared header replaces the app's own, never duplicates it
    own = c.get("/api/iso/sets-its-own")
    assert own.headers.get_list("cross-origin-opener-policy") == ["same-origin"]


def test_onnx_is_binary_and_not_gzipped(client):
    r = client.get("/iso/lab/tiny.onnx", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-type"] == "application/octet-stream"
    assert "content-encoding" not in r.headers


def test_websocket_and_lifespan_pass_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    import asyncio

    mw = ResponseHeadersMiddleware(inner, rules=[("/a", "/a", {"X-A": "1"})])
    asyncio.run(mw({"type": "websocket", "path": "/a"}, None, None))
    asyncio.run(mw({"type": "lifespan"}, None, None))
    assert seen == ["websocket", "lifespan"]


def _config(**fields):
    defaults = {"name": "x", "route_prefix": "/api/x", "app_type": "asgi_app"}
    return AppConfig(**{**defaults, **fields})


def test_rules_cover_both_mounts_and_skip_a_root_route():
    app = _config(cross_origin_isolated=["lab"])
    assert [p for _, p, _ in header_rules_for_app(app)] == ["/x/lab", "/api/x/lab"]
    rooted = _config(route_prefix="/", response_headers={"/": {"X-A": "1"}})
    assert [p for _, p, _ in header_rules_for_app(rooted)] == ["/x"]


def test_defaults_declare_nothing():
    app = _config()
    assert app.cross_origin_isolated is False and app.response_headers == {}
    assert header_rules_for_app(app) == []
    assert add_response_headers(None, PlatformConfig(apps=[app])) is False


@pytest.mark.parametrize(
    "field, value",
    [
        ("cross_origin_isolated", "lab"),  # a string, not a list
        ("cross_origin_isolated", {"lab": 1}),  # a TOML inline table
        ("response_headers", {"lab": {"X-A": "café"}}),  # not ASCII: 500s later
        ("response_headers", {"lab": {"Set-Cookie": "enlace_session=x"}}),
        ("response_headers", {"lab": {"Content-Length": "0"}}),
        ("response_headers", {"/": {"Clear-Site-Data": '"*"'}}),
        ("response_headers", {"/": {"strict-transport-security": "max-age=1"}}),
        ("cross_origin_isolated", ["../up"]),
        ("cross_origin_isolated", ["lab", "/lab/"]),  # the same path twice
        ("response_headers", {"lab": {"Bad Name": "x"}}),
        ("response_headers", {"lab": {"X-A": "one\r\nSet-Cookie: x=1"}}),
        ("response_headers", {"lab": {"X-A": 1}}),
        ("response_headers", {"lab": {}}),
        ("response_headers", {"lab": {"X-A": "1", "x-a": "2"}}),
        ("response_headers", {"lab": {"X-A": "1"}, "/lab": {"X-B": "2"}}),
        ("response_headers", {"lab?q": {"X-A": "1"}}),
        ("response_headers", ["lab"]),
    ],
)
def test_bad_declarations_fail_loudly(field, value):
    with pytest.raises(ValueError):
        _config(**{field: value})


def test_app_toml_maps_onto_app_config(tmp_path):
    apps = tmp_path / "apps"
    apps.mkdir()
    _app(
        apps,
        "iso",
        """\
        cross_origin_isolated = ["/lab/"]

        [response_headers]
        "lab/models" = { "X-A" = "1" }
        """,
    )
    (app,) = discover_apps(PlatformConfig(apps_dir=apps)).apps
    assert app.cross_origin_isolated == ["lab"]
    assert app.response_headers == {"lab/models": {"X-A": "1"}}
    assert app.provenance["cross_origin_isolated"] == "override: app.toml"


def test_one_apps_rules_never_reach_another_apps_paths(tmp_path):
    """An app whose mount is a prefix of another's (``api`` vs ``/api/other``)."""
    apps = tmp_path / "apps"
    apps.mkdir()
    _app(apps, "api", "cross_origin_isolated = true\n")
    _app(apps, "other")
    c = TestClient(build_backend(discover_apps(PlatformConfig(apps_dir=apps))))
    assert _coi(c.get("/api/")) == COI  # its own page
    assert _coi(c.get("/api/other/other")) == {}  # the other app's API
    assert _coi(c.get("/other/lab/")) == {}


def test_the_sign_in_gate_response_carries_the_headers(tmp_path):
    """Outside the plugins: a gated route's refusal is isolated too, so a smoke check
    can read the headers without signing in."""
    from starlette.responses import RedirectResponse

    class Gate:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope["path"].startswith("/iso/"):
                await RedirectResponse("/auth/login", 303)(scope, receive, send)
                return
            await self.app(scope, receive, send)

    def gate_plugin(parent, cfg):
        parent.add_middleware(Gate)

    apps = tmp_path / "apps"
    apps.mkdir()
    _app(apps, "iso", 'cross_origin_isolated = ["lab"]\n')
    config = discover_apps(PlatformConfig(apps_dir=apps))
    c = TestClient(build_backend(config, plugins=[gate_plugin]))
    r = c.get("/iso/lab/", follow_redirects=False)
    assert r.status_code == 303
    assert _coi(r) == COI
    assert _coi(c.get("/iso/", follow_redirects=False)) == {}


def test_a_root_route_is_warned_about(caplog):
    with caplog.at_level("WARNING", logger="enlace.response_headers"):
        header_rules_for_app(_config(route_prefix="/", cross_origin_isolated=True))
    assert "site root" in caplog.text


def test_media_types_extend_the_defaults(tmp_path):
    from enlace.frontend import RevalidatingStaticFiles

    d = tmp_path / "f"
    d.mkdir()
    (d / "m.onnx").write_bytes(b"x")
    (d / "w.gguf").write_bytes(b"x")
    files = RevalidatingStaticFiles(
        directory=str(d), media_types={".gguf": "application/octet-stream"}
    )
    c = TestClient(files)
    assert c.get("/m.onnx").headers["content-type"] == "application/octet-stream"
    assert c.get("/w.gguf").headers["content-type"] == "application/octet-stream"


@pytest.mark.parametrize(
    "name", ["Access-Control-Allow-Origin", "access-control-allow-credentials", "NEL",
             "Alt-Svc", "Origin-Agent-Cluster", "Accept-CH"]
)
def test_cors_and_origin_wide_headers_are_refused_by_name(name):
    """Pinned on the module's own message: pydantic's type errors are ValueErrors too."""
    with pytest.raises(ValueError, match="cannot be declared"):
        _config(response_headers={"/": {name: "x"}})
