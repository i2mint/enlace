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

    mw = ResponseHeadersMiddleware(inner, rules=[("/a", {"X-A": "1"})])
    asyncio.run(mw({"type": "websocket", "path": "/a"}, None, None))
    asyncio.run(mw({"type": "lifespan"}, None, None))
    assert seen == ["websocket", "lifespan"]


def _config(**fields):
    defaults = {"name": "x", "route_prefix": "/api/x", "app_type": "asgi_app"}
    return AppConfig(**{**defaults, **fields})


def test_rules_cover_both_mounts_and_skip_a_root_route():
    app = _config(cross_origin_isolated=["lab"])
    assert [p for p, _ in header_rules_for_app(app)] == ["/x/lab", "/api/x/lab"]
    rooted = _config(route_prefix="/", response_headers={"/": {"X-A": "1"}})
    assert [p for p, _ in header_rules_for_app(rooted)] == ["/x"]


def test_defaults_declare_nothing():
    app = _config()
    assert app.cross_origin_isolated is False and app.response_headers == {}
    assert header_rules_for_app(app) == []
    assert add_response_headers(None, PlatformConfig(apps=[app])) is False


@pytest.mark.parametrize(
    "field, value",
    [
        ("cross_origin_isolated", "lab"),  # a string, not a list
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
