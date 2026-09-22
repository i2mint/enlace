"""Tests for HTML revalidation headers on enlace's static mounts.

Without ``Cache-Control``, browsers heuristically cache HTML and keep loading a
previous build (whose HTML names the old cache-busted asset URLs) after a
deploy. These tests pin the rule: HTML documents — direct, SPA fallback, 304 —
carry ``Cache-Control: no-cache``; non-HTML assets are left alone.
"""

from starlette.testclient import TestClient

from enlace.frontend import (
    DFLT_HTML_CACHE_CONTROL,
    LandingWithUnknownApp404,
    RevalidatingStaticFiles,
    SPAStaticFiles,
)


def _frontend(tmp_path):
    """A minimal built frontend: index, a nested page, a JS asset, a Next shell."""
    d = tmp_path / "frontend"
    (d / "en").mkdir(parents=True)
    (d / "projects" / "_").mkdir(parents=True)  # Next.js RSC dir beside _.html
    (d / "index.html").write_text("<!doctype html><title>Home</title>")
    (d / "en" / "page.html").write_text("<!doctype html><title>Page</title>")
    (d / "projects" / "_.html").write_text("<!doctype html><title>Shell</title>")
    (d / "app.js").write_text("console.log('hi')")
    return d


def _client(tmp_path, cls=SPAStaticFiles, **kwargs):
    return TestClient(cls(directory=str(_frontend(tmp_path)), html=True, **kwargs))


def test_default_is_no_cache():
    assert DFLT_HTML_CACHE_CONTROL == "no-cache"


def test_index_html_is_revalidated(tmp_path):
    r = _client(tmp_path).get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"


def test_nested_html_page_is_revalidated(tmp_path):
    r = _client(tmp_path).get("/en/page.html")
    assert r.headers["cache-control"] == "no-cache"


def test_spa_fallback_is_revalidated(tmp_path):
    r = _client(tmp_path).get("/some/client/route")
    assert r.status_code == 200
    assert "Home" in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_next_wildcard_shell_is_revalidated(tmp_path):
    r = _client(tmp_path).get("/projects/abc123")
    assert "Shell" in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_assets_are_left_alone(tmp_path):
    r = _client(tmp_path).get("/app.js?v=abc")
    assert r.status_code == 200
    assert "cache-control" not in r.headers


def test_304_keeps_cache_control(tmp_path):
    client = _client(tmp_path)
    etag = client.get("/").headers["etag"]
    r = client.get("/", headers={"if-none-match": etag})
    assert r.status_code == 304
    assert r.headers["cache-control"] == "no-cache"


def test_opt_out(tmp_path):
    r = _client(tmp_path, html_cache_control=None).get("/")
    assert "cache-control" not in r.headers


def test_custom_value(tmp_path):
    value = "no-cache, must-revalidate"
    r = _client(tmp_path, cls=RevalidatingStaticFiles, html_cache_control=value).get(
        "/en/page.html"
    )
    assert r.headers["cache-control"] == value


def test_landing_is_revalidated(tmp_path):
    app = LandingWithUnknownApp404(landing_dir=_frontend(tmp_path))
    client = TestClient(app)
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/app.js").headers


def test_composed_platform_revalidates_app_html(tmp_path):
    """End to end: a frontend-only app mounted by ``build_backend``."""
    from enlace.base import PlatformConfig
    from enlace.compose import build_backend
    from enlace.discover import discover_apps

    apps_dir = tmp_path / "apps"
    app_dir = apps_dir / "site"
    app_dir.mkdir(parents=True)
    _frontend(app_dir)  # creates site/frontend/
    config = discover_apps(PlatformConfig(apps_dir=apps_dir))
    client = TestClient(build_backend(config))

    page = client.get("/site/en/page.html")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    asset = client.get("/site/app.js")
    assert asset.status_code == 200
    assert "cache-control" not in asset.headers
