"""Tests for enlace.compose — ASGI app composition."""

from starlette.testclient import TestClient

from enlace.base import PlatformConfig
from enlace.compose import build_backend
from enlace.discover import discover_apps


def test_build_backend_mounts_single_app(single_app_dir):
    """A single discovered app is mounted and responds at its prefix."""
    config = PlatformConfig(apps_dir=single_app_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    resp = client.get("/api/foo/hello")
    assert resp.status_code == 200
    assert resp.json() == {"message": "Hello from foo"}


def test_build_backend_mounts_multiple_apps(multi_app_dir):
    """Multiple apps are mounted and each responds at its own prefix."""
    config = PlatformConfig(apps_dir=multi_app_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    for name in ["alpha", "beta", "gamma"]:
        resp = client.get(f"/api/{name}/hello")
        assert resp.status_code == 200
        assert resp.json() == {"message": f"Hello from {name}"}


def test_build_backend_health_endpoint(single_app_dir):
    """Sub-app endpoints beyond the first are also reachable."""
    config = PlatformConfig(apps_dir=single_app_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    resp = client.get("/api/foo/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_cors_headers_present(single_app_dir):
    """CORS headers are present on responses (middleware applied on parent)."""
    config = PlatformConfig(apps_dir=single_app_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    resp = client.options(
        "/api/foo/hello",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" in resp.headers


def test_unmatched_route_returns_404(single_app_dir):
    """Requests to non-existent routes return 404."""
    config = PlatformConfig(apps_dir=single_app_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    resp = client.get("/api/nonexistent/hello")
    assert resp.status_code == 404


def test_build_backend_functions_app(tmp_apps_dir):
    """A functions-type app has its functions exposed as endpoints."""
    from enlace.tests.conftest import FUNCTIONS_MODULE

    func_dir = tmp_apps_dir / "calc"
    func_dir.mkdir()
    (func_dir / "server.py").write_text(FUNCTIONS_MODULE)

    config = PlatformConfig(apps_dir=tmp_apps_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    resp = client.post("/api/calc/greet?name=World")
    assert resp.status_code == 200
    assert resp.json() == {"greeting": "Hello, World!"}


def test_build_backend_multi_source(multi_source_dirs):
    """Apps from multiple source directories are all mounted and respond."""
    source_a, source_b = multi_source_dirs
    config = PlatformConfig(apps_dirs=[source_a, source_b])
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    for name in ["alpha", "beta", "gamma", "delta"]:
        resp = client.get(f"/api/{name}/hello")
        assert resp.status_code == 200
        assert resp.json() == {"message": f"Hello from {name}"}


def test_build_backend_standalone_app(standalone_app_dir):
    """An individual app directory works with composition."""
    config = PlatformConfig(app_dirs=[standalone_app_dir])
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    resp = client.get("/api/my_standalone_app/hello")
    assert resp.status_code == 200
    assert resp.json() == {"message": "Hello from my_standalone_app"}
