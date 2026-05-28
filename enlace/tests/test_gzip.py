"""Tests for GZip compression of platform responses.

GZipMiddleware itself is Starlette's; these tests verify enlace wires it in
correctly — outermost, with the right minimum_size, so large frontend bundles
go out compressed while small API responses are left alone.
"""

from starlette.testclient import TestClient

from enlace.base import PlatformConfig
from enlace.compose import build_backend
from enlace.discover import discover_apps


def _app_with_large_frontend(tmp_apps_dir):
    foo = tmp_apps_dir / "foo"
    foo.mkdir()
    frontend = foo / "frontend"
    frontend.mkdir()
    # Well over the 1024-byte minimum and highly compressible.
    big = "<html><head></head><body>" + ("data " * 2000) + "</body></html>"
    (frontend / "index.html").write_text(big)
    (frontend / "data.json").write_text('{"items": [' + ("0," * 2000) + "0]}")
    config = PlatformConfig(apps_dir=tmp_apps_dir)
    config = discover_apps(config)
    return build_backend(config)


def test_large_html_is_gzipped(tmp_apps_dir):
    app = _app_with_large_frontend(tmp_apps_dir)
    client = TestClient(app)
    resp = client.get("/foo/", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    # httpx transparently decodes; the body is intact.
    assert "data data" in resp.text


def test_large_json_asset_is_gzipped(tmp_apps_dir):
    app = _app_with_large_frontend(tmp_apps_dir)
    client = TestClient(app)
    resp = client.get("/foo/data.json", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"


def test_vary_header_set_when_gzipped(tmp_apps_dir):
    app = _app_with_large_frontend(tmp_apps_dir)
    client = TestClient(app)
    resp = client.get("/foo/", headers={"Accept-Encoding": "gzip"})
    assert "accept-encoding" in resp.headers.get("vary", "").lower()


def test_no_compression_without_accept_encoding(tmp_apps_dir):
    app = _app_with_large_frontend(tmp_apps_dir)
    client = TestClient(app)
    resp = client.get("/foo/", headers={"Accept-Encoding": "identity"})
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers


def test_small_api_response_not_compressed(single_app_dir):
    config = PlatformConfig(apps_dir=single_app_dir)
    config = discover_apps(config)
    app = build_backend(config)
    client = TestClient(app)
    resp = client.get("/api/foo/hello", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    # Well under minimum_size=1024 → streamed uncompressed.
    assert "content-encoding" not in resp.headers


def test_deploy_headers_survive_compression(single_app_dir, tmp_path):
    """X-Deploy-* headers (added innermost) are preserved through GZip."""
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    (manifest_dir / "foo.json").write_text('{"app": "foo"}')
    config = PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    config = discover_apps(config)
    app = build_backend(config)
    client = TestClient(app)
    resp = client.get("/api/foo/hello", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("x-deploy-app") == "foo"
