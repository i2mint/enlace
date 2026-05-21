"""Tests for ``LandingWithUnknownApp404`` — the friendly unknown-path 404 page.

The landing app is mounted at ``/`` as a catch-all; without this wrapper an
unknown top-level path silently served the landing ``index.html`` (HTTP 200),
disguising broken links. These tests pin the corrected behavior: real files
still serve, unknown paths get an explanatory 404 with somewhere to go next.
"""

from starlette.testclient import TestClient

from enlace.frontend import LandingWithUnknownApp404


def _landing(tmp_path):
    """Create a minimal landing directory with an index and one asset."""
    d = tmp_path / "landing"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>Home</title>Home")
    (d / "app.js").write_text("console.log('hi')")
    return d


def test_root_serves_index(tmp_path):
    app = LandingWithUnknownApp404(landing_dir=_landing(tmp_path))
    r = TestClient(app).get("/")
    assert r.status_code == 200
    assert "Home" in r.text


def test_real_asset_is_served(tmp_path):
    app = LandingWithUnknownApp404(landing_dir=_landing(tmp_path))
    r = TestClient(app).get("/app.js")
    assert r.status_code == 200
    assert "console.log" in r.text


def test_unknown_path_returns_friendly_404(tmp_path):
    app = LandingWithUnknownApp404(landing_dir=_landing(tmp_path))
    r = TestClient(app).get("/nope-not-here")
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Page not found" in r.text
    assert "/nope-not-here" in r.text  # echoes the path the user typed
    assert "/auth/login" in r.text  # suggests signing in as a way forward


def test_unknown_path_post_returns_plain_404(tmp_path):
    app = LandingWithUnknownApp404(landing_dir=_landing(tmp_path))
    r = TestClient(app).post("/nope-not-here")
    assert r.status_code == 404
