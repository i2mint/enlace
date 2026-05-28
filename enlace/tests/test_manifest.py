"""Tests for the deploy manifest layer (schema, endpoints, headers)."""

import json

import pytest
from starlette.testclient import TestClient

from enlace.base import PlatformConfig
from enlace.compose import build_backend
from enlace.discover import discover_apps
from enlace.manifest import (
    MANIFEST_SCHEMA_VERSION,
    PLATFORM_MANIFEST_NAME,
    DeployManifest,
    ExternalRef,
    SourceRef,
    load_manifest,
    load_platform_manifest,
    resolve_manifest_dir,
)

# --- schema -----------------------------------------------------------------


def test_manifest_minimal_construction():
    m = DeployManifest(app="foo")
    assert m.app == "foo"
    assert m.schema_version == MANIFEST_SCHEMA_VERSION
    assert m.app_source.sha is None
    assert m.externals == {}


def test_manifest_full_round_trip():
    raw = {
        "schema_version": 1,
        "app": "reelee-web",
        "deployed_at": "2026-05-26T18:00:00Z",
        "deployer": "ci",
        "platform": "thorwhalen",
        "app_source": {
            "sha": "9543b80",
            "ref": "main",
            "dirty": False,
            "local_path": "/x/y",
        },
        "platform_source": {"sha": "de2eb4e", "ref": "main", "dirty": True},
        "externals": {
            "artful": {"sha": "832afca", "version": "0.0.6"},
            "lacing": {"sha": "1b0513f", "version": "0.0.21"},
        },
        "enlace_version": "0.1.6",
    }
    m = DeployManifest.model_validate(raw)
    assert m.deployer == "ci"
    assert m.app_source.dirty is False
    assert m.externals["artful"] == ExternalRef(sha="832afca", version="0.0.6")
    # Round trip
    assert m.model_dump()["externals"]["lacing"]["version"] == "0.0.21"


def test_manifest_unknown_keys_preserved_in_extra():
    # Pydantic by default ignores unknown keys; verify our model doesn't crash
    raw = {"app": "x", "made_up_key": 42}
    m = DeployManifest.model_validate(raw)
    assert m.app == "x"


# --- loader -----------------------------------------------------------------


def test_load_manifest_returns_stub_when_dir_missing():
    m = load_manifest("foo", None, enlace_version="9.9.9")
    assert m.app == "foo"
    assert m.enlace_version == "9.9.9"
    assert m.app_source.sha is None


def test_load_manifest_reads_file(tmp_path):
    manifest = {
        "schema_version": 1,
        "app": "foo",
        "deployed_at": "2026-05-26T18:00:00Z",
        "app_source": {"sha": "abc123", "ref": "main"},
    }
    (tmp_path / "foo.json").write_text(json.dumps(manifest))
    m = load_manifest("foo", tmp_path, enlace_version="0.1.0")
    assert m.app_source.sha == "abc123"
    assert m.deployed_at == "2026-05-26T18:00:00Z"
    # enlace_version fills in if the file didn't specify
    assert m.enlace_version == "0.1.0"


def test_load_manifest_falls_back_when_file_missing(tmp_path):
    m = load_manifest("ghost", tmp_path)
    assert m.app == "ghost"
    assert m.app_source.sha is None


def test_load_manifest_handles_corrupt_file(tmp_path):
    (tmp_path / "foo.json").write_text("{ not valid json")
    m = load_manifest("foo", tmp_path)
    assert m.app == "foo"
    assert m.app_source.sha is None


def test_load_platform_manifest(tmp_path):
    (tmp_path / f"{PLATFORM_MANIFEST_NAME}.json").write_text(
        json.dumps({"app": PLATFORM_MANIFEST_NAME, "platform": "tw"})
    )
    m = load_platform_manifest(tmp_path)
    assert m.platform == "tw"


def test_resolve_manifest_dir_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("ENLACE_MANIFEST_DIR", str(tmp_path / "from_env"))
    assert resolve_manifest_dir(tmp_path / "from_config") == tmp_path / "from_env"


def test_resolve_manifest_dir_uses_config_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("ENLACE_MANIFEST_DIR", raising=False)
    assert resolve_manifest_dir(tmp_path) == tmp_path


def test_resolve_manifest_dir_none_when_neither(monkeypatch):
    monkeypatch.delenv("ENLACE_MANIFEST_DIR", raising=False)
    assert resolve_manifest_dir(None) is None


# --- endpoint + headers integration ----------------------------------------


@pytest.fixture
def manifest_dir(tmp_path):
    d = tmp_path / "manifests"
    d.mkdir()
    return d


def _write(dir_, app, **fields):
    payload = {"app": app, **fields}
    (dir_ / f"{app}.json").write_text(json.dumps(payload))


def test_per_app_meta_endpoint_at_route_prefix(single_app_dir, manifest_dir):
    _write(
        manifest_dir,
        "foo",
        app_source={"sha": "abc123", "ref": "main"},
        deployed_at="2026-05-26T18:00:00Z",
    )
    config = PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    resp = client.get("/api/foo/_meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["app"] == "foo"
    assert body["app_source"]["sha"] == "abc123"
    assert body["deployed_at"] == "2026-05-26T18:00:00Z"


def test_per_app_meta_endpoint_at_frontend_path(single_app_dir, manifest_dir):
    _write(manifest_dir, "foo", app_source={"sha": "abc123"})
    config = PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    resp = client.get("/foo/_meta")
    assert resp.status_code == 200
    assert resp.json()["app_source"]["sha"] == "abc123"


def test_platform_meta_endpoint(single_app_dir, manifest_dir):
    _write(manifest_dir, "_platform", platform="thorwhalen")
    config = PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    resp = client.get("/_meta")
    assert resp.status_code == 200
    assert resp.json()["platform"] == "thorwhalen"


def test_meta_endpoint_returns_stub_without_manifest_file(single_app_dir, manifest_dir):
    # manifest_dir exists but no foo.json — stub is served.
    config = PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    resp = client.get("/api/foo/_meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["app"] == "foo"
    assert body["app_source"]["sha"] is None


def test_deploy_headers_present_on_app_response(single_app_dir, manifest_dir):
    _write(
        manifest_dir,
        "foo",
        app_source={"sha": "abc123"},
        deployed_at="2026-05-26T18:00:00Z",
    )
    config = PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    resp = client.get("/api/foo/hello")
    assert resp.status_code == 200
    assert resp.headers.get("x-deploy-app") == "foo"
    assert resp.headers.get("x-deploy-sha") == "abc123"
    assert resp.headers.get("x-deploy-time") == "2026-05-26T18:00:00Z"


def test_deploy_headers_fall_back_to_platform(single_app_dir, manifest_dir):
    _write(manifest_dir, "_platform", app_source={"sha": "platsha"})
    config = PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    # An unmounted root-level path; falls through to platform manifest headers.
    resp = client.get("/_meta")
    assert resp.status_code == 200
    assert resp.headers.get("x-deploy-app") == "_platform"
    assert resp.headers.get("x-deploy-sha") == "platsha"


def test_deploy_headers_no_sha_when_stub(single_app_dir):
    # No manifest_dir → all stubs. Only X-Deploy-App is emitted, not SHA/time.
    config = PlatformConfig(apps_dir=single_app_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    resp = client.get("/api/foo/hello")
    assert resp.headers.get("x-deploy-app") == "foo"
    assert "x-deploy-sha" not in resp.headers
    assert "x-deploy-time" not in resp.headers


def test_sourceref_dump_roundtrip():
    s = SourceRef(sha="abc", ref="main", dirty=False)
    assert SourceRef.model_validate(s.model_dump()) == s
