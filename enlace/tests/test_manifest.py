"""Tests for the deploy manifest layer (schema, endpoints, headers)."""

import json
import os
from datetime import datetime

import pytest
from starlette.testclient import TestClient

from enlace.base import PlatformConfig
from enlace.compose import build_backend
from enlace.discover import discover_apps
from enlace.manifest import (
    MANIFEST_ERROR_KEY,
    MANIFEST_SCHEMA_VERSION,
    PLATFORM_MANIFEST_NAME,
    DeployManifest,
    DeployMetaTagMiddleware,
    ExternalRef,
    SourceRef,
    _inject_meta,
    _meta_snippet,
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
    assert m.extra[MANIFEST_ERROR_KEY].startswith("JSONDecodeError")


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


def test_platform_meta_reflects_manifest_rewritten_after_startup(
    single_app_dir, manifest_dir
):
    """/_meta must re-read the manifest instead of serving a process-start snapshot.

    See i2mint/enlace#42: jobs other than a full deploy legitimately rewrite the
    platform manifest while the backend is running, and a snapshot taken at
    compose time reports the state the box left, with nothing in the payload to
    say so.
    """
    _write(manifest_dir, PLATFORM_MANIFEST_NAME, app_source={"sha": "oldsha"})
    config = PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    config = discover_apps(config)
    client = TestClient(build_backend(config))

    assert client.get("/_meta").json()["app_source"]["sha"] == "oldsha"

    _write(manifest_dir, PLATFORM_MANIFEST_NAME, app_source={"sha": "newsha"})
    # Push the mtime forward explicitly: two writes this close together can land
    # inside one filesystem timestamp tick, which would make the test flaky.
    path = manifest_dir / f"{PLATFORM_MANIFEST_NAME}.json"
    st = path.stat()
    os.utime(path, (st.st_atime + 2, st.st_mtime + 2))

    assert client.get("/_meta").json()["app_source"]["sha"] == "newsha"


def test_platform_meta_survives_a_schema_invalid_rewrite(single_app_dir, manifest_dir):
    """A rewrite /_meta can't validate must degrade, not turn /_meta into a 500.

    Re-reading at request time means any job that rewrites the manifest can now
    reach this code path. A value the schema rejects (here an unknown
    ``deployer``) degrades to the stub, the same way a corrupt JSON file does,
    and recovers as soon as a valid manifest is written back.
    """
    _write(manifest_dir, PLATFORM_MANIFEST_NAME, platform="thorwhalen")
    config = PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    config = discover_apps(config)
    client = TestClient(build_backend(config))
    assert client.get("/_meta").json()["platform"] == "thorwhalen"

    path = manifest_dir / f"{PLATFORM_MANIFEST_NAME}.json"
    _write(manifest_dir, PLATFORM_MANIFEST_NAME, deployer="connector-refresh")
    st = path.stat()
    os.utime(path, (st.st_atime + 2, st.st_mtime + 2))

    resp = client.get("/_meta")
    assert resp.status_code == 200
    assert resp.json()["platform"] is None  # the stub: nothing valid to report
    assert resp.json()["manifest_mtime"] is not None
    # ...but it says why, instead of passing for "no manifest yet".
    assert "deployer" in resp.json()["extra"][MANIFEST_ERROR_KEY]

    _write(manifest_dir, PLATFORM_MANIFEST_NAME, platform="thorwhalen", deployer="ci")
    os.utime(path, (st.st_atime + 4, st.st_mtime + 4))
    healthy = client.get("/_meta").json()
    assert healthy["platform"] == "thorwhalen"
    assert MANIFEST_ERROR_KEY not in healthy["extra"]


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"app": PLATFORM_MANIFEST_NAME, "deployer": "connector-refresh"}),
        "[]",  # valid JSON, but not an object
        "{ not valid json",
    ],
    ids=["schema-invalid", "non-object", "corrupt"],
)
@pytest.mark.parametrize("which", [PLATFORM_MANIFEST_NAME, "foo"])
def test_backend_starts_despite_an_unusable_manifest(
    single_app_dir, manifest_dir, content, which
):
    """A bad manifest must not stop the platform from starting.

    #50 degraded only the request-time re-read of the platform manifest, but
    ``build_backend`` loads the platform and every app manifest at compose
    time, so the same schema-invalid file raised there and the whole backend
    failed to start. A non-object JSON file raised ``AttributeError`` on both
    paths.
    """
    (manifest_dir / f"{which}.json").write_text(content)
    config = discover_apps(
        PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    )
    client = TestClient(build_backend(config))
    meta_path = "/_meta" if which == PLATFORM_MANIFEST_NAME else "/api/foo/_meta"
    resp = client.get(meta_path)
    assert resp.status_code == 200
    error = resp.json()["extra"][MANIFEST_ERROR_KEY]
    assert str(manifest_dir) not in error  # served publicly: no server paths
    assert "connector-refresh" not in error  # nor the rejected input values


def test_load_manifest_flags_non_object_json(tmp_path):
    (tmp_path / "foo.json").write_text('"just a string"')
    m = load_manifest("foo", tmp_path)
    assert m.app == "foo"
    assert m.extra[MANIFEST_ERROR_KEY].startswith("ValueError")


def test_platform_meta_picks_up_a_manifest_written_after_startup(
    single_app_dir, manifest_dir
):
    # manifest_dir exists but is empty at compose time: the stub is served, and
    # the real manifest is picked up once it appears.
    config = PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    config = discover_apps(config)
    client = TestClient(build_backend(config))

    assert client.get("/_meta").json()["platform"] is None

    _write(manifest_dir, PLATFORM_MANIFEST_NAME, platform="thorwhalen")

    assert client.get("/_meta").json()["platform"] == "thorwhalen"


def test_platform_meta_reports_manifest_mtime(single_app_dir, manifest_dir):
    _write(manifest_dir, PLATFORM_MANIFEST_NAME, platform="thorwhalen")
    config = PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    config = discover_apps(config)
    client = TestClient(build_backend(config))

    reported = client.get("/_meta").json()["manifest_mtime"]
    assert reported is not None
    parsed = datetime.fromisoformat(reported.replace("Z", "+00:00"))
    on_disk = (manifest_dir / f"{PLATFORM_MANIFEST_NAME}.json").stat().st_mtime
    assert abs(parsed.timestamp() - on_disk) < 1


def test_platform_meta_mtime_is_none_without_manifest_file(
    single_app_dir, manifest_dir
):
    # manifest_dir is configured but holds no platform manifest: the payload
    # still carries the key, so a reader never has to guess whether it exists.
    config = PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    config = discover_apps(config)
    client = TestClient(build_backend(config))

    assert client.get("/_meta").json()["manifest_mtime"] is None


def test_platform_meta_mtime_is_none_without_manifest_dir(single_app_dir, monkeypatch):
    monkeypatch.delenv("ENLACE_MANIFEST_DIR", raising=False)
    config = PlatformConfig(apps_dir=single_app_dir)
    config = discover_apps(config)
    client = TestClient(build_backend(config))

    assert client.get("/_meta").json()["manifest_mtime"] is None


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


# --- meta-tag injection: unit helpers ---------------------------------------


def test_meta_snippet_none_when_nothing_to_inject():
    assert _meta_snippet(DeployManifest(app="foo")) is None


def test_meta_snippet_includes_sha_and_time():
    m = DeployManifest(
        app="foo",
        app_source=SourceRef(sha="abc123"),
        deployed_at="2026-05-26T18:00:00Z",
    )
    snippet = _meta_snippet(m)
    assert b'<meta name="x-deploy-sha" content="abc123">' in snippet
    assert b'<meta name="x-deploy-time" content="2026-05-26T18:00:00Z">' in snippet


def test_meta_snippet_only_sha():
    m = DeployManifest(app="foo", app_source=SourceRef(sha="abc123"))
    snippet = _meta_snippet(m)
    assert b"x-deploy-sha" in snippet
    assert b"x-deploy-time" not in snippet


def test_meta_snippet_escapes_content():
    m = DeployManifest(app="foo", deployed_at='"><script>x</script>')
    snippet = _meta_snippet(m)
    assert b"<script>" not in snippet
    assert b"&lt;script&gt;" in snippet


def test_inject_meta_before_head_close():
    body = b"<html><head><title>t</title></head><body>hi</body></html>"
    out = _inject_meta(body, b"<meta>")
    assert out == b"<html><head><title>t</title><meta></head><body>hi</body></html>"


def test_inject_meta_after_head_open_when_no_close():
    body = b"<html><head><body>hi"
    out = _inject_meta(body, b"<meta>")
    assert out == b"<html><head><meta><body>hi"


def test_inject_meta_prepends_when_no_head():
    body = b"<body>hi</body>"
    out = _inject_meta(body, b"<meta>")
    assert out == b"<meta><body>hi</body>"


def test_inject_meta_head_close_case_insensitive():
    body = b"<HTML><HEAD></HEAD></HTML>"
    out = _inject_meta(body, b"<meta>")
    assert out == b"<HTML><HEAD><meta></HEAD></HTML>"


# --- meta-tag injection: middleware -----------------------------------------


def _html_asgi_app(body: bytes, content_type: bytes = b"text/html; charset=utf-8"):
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", content_type),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app


def _streaming_html_app(chunks: list[bytes]):
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/html")],
            }
        )
        for i, chunk in enumerate(chunks):
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": i < len(chunks) - 1,
                }
            )

    return app


_MANIFEST = DeployManifest(
    app="foo",
    app_source=SourceRef(sha="abc123"),
    deployed_at="2026-05-26T18:00:00Z",
)


def _wrap(app):
    return DeployMetaTagMiddleware(app, manifests_by_prefix={"/foo": _MANIFEST})


def test_middleware_injects_into_html():
    inner = _html_asgi_app(b"<html><head></head><body>hi</body></html>")
    client = TestClient(_wrap(inner))
    resp = client.get("/foo/")
    assert resp.status_code == 200
    assert '<meta name="x-deploy-sha" content="abc123">' in resp.text
    assert "x-deploy-time" in resp.text
    # Content-Length must match the rewritten body.
    assert int(resp.headers["content-length"]) == len(resp.content)


def test_middleware_handles_streaming_body():
    inner = _streaming_html_app([b"<html><head></head>", b"<body>hi</body></html>"])
    client = TestClient(_wrap(inner))
    resp = client.get("/foo/")
    assert "x-deploy-sha" in resp.text
    assert resp.text.count("<meta") == 2


def test_middleware_leaves_json_untouched():
    inner = _html_asgi_app(b'{"a": 1}', content_type=b"application/json")
    client = TestClient(_wrap(inner))
    resp = client.get("/foo/api")
    assert resp.text == '{"a": 1}'
    assert "x-deploy-sha" not in resp.text


def test_middleware_noop_when_manifest_has_no_sha():
    stub = DeployManifest(app="foo")
    inner = _html_asgi_app(b"<html><head></head></html>")
    mw = DeployMetaTagMiddleware(inner, manifests_by_prefix={"/foo": stub})
    client = TestClient(mw)
    resp = client.get("/foo/")
    assert "<meta" not in resp.text


def test_middleware_passes_through_unmatched_path():
    inner = _html_asgi_app(b"<html><head></head></html>")
    mw = DeployMetaTagMiddleware(
        inner, manifests_by_prefix={"/foo": _MANIFEST}, platform_manifest=None
    )
    client = TestClient(mw)
    resp = client.get("/other/")
    assert "<meta" not in resp.text


def test_meta_injected_end_to_end_via_frontend(tmp_apps_dir, manifest_dir):
    foo = tmp_apps_dir / "foo"
    foo.mkdir()
    frontend = foo / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        "<html><head><title>Foo</title></head><body>hi</body></html>"
    )
    _write(manifest_dir, "foo", app_source={"sha": "deadbee"})

    config = PlatformConfig(apps_dir=tmp_apps_dir, manifest_dir=manifest_dir)
    config = discover_apps(config)
    app = build_backend(config)

    client = TestClient(app)
    resp = client.get("/foo/")
    assert resp.status_code == 200
    assert '<meta name="x-deploy-sha" content="deadbee">' in resp.text


def test_deploy_headers_percent_encode_a_non_ascii_app_name(tmp_path, manifest_dir):
    """#58: an app dir named ``café`` is served and its header stays ASCII."""
    from urllib.parse import unquote

    apps = tmp_path / "apps"
    (apps / "café" / "frontend").mkdir(parents=True)
    (apps / "café" / "frontend" / "index.html").write_text("<p>bonjour</p>")
    _write(manifest_dir, "café", deployed_at="2026-05-26T18:00:00Z")
    config = discover_apps(PlatformConfig(apps_dir=apps, manifest_dir=manifest_dir))
    client = TestClient(build_backend(config))

    resp = client.get("/café/")  # TestClient (httpx) raised here before the fix

    assert resp.status_code == 200
    assert "bonjour" in resp.text
    assert resp.headers["x-deploy-app"] == "caf%C3%A9"
    assert unquote(resp.headers["x-deploy-app"]) == "café"
    assert resp.headers["x-deploy-time"] == "2026-05-26T18:00:00Z"


# --- the running enlace version (#78) ----------------------------------------


def test_meta_reports_the_running_enlace_even_when_the_manifest_is_stale(
    single_app_dir, manifest_dir, monkeypatch
):
    """The deploy manifest's enlace_version is what was deployed; after an
    engine-only upgrade it names the old version. enlace_runtime_version is
    always what is running, on the platform and the per-app /_meta."""
    from enlace import compose
    from enlace.manifest import RUNTIME_VERSION_KEY

    monkeypatch.setattr(compose, "_detect_enlace_version", lambda: "9.9.9")
    _write(manifest_dir, PLATFORM_MANIFEST_NAME, enlace_version="0.0.1")
    _write(manifest_dir, "foo", enlace_version="0.0.1")
    config = discover_apps(
        PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    )
    client = TestClient(build_backend(config))

    for path in ("/_meta", "/api/foo/_meta", "/foo/_meta"):
        payload = client.get(path).json()
        assert payload["enlace_version"] == "0.0.1", path  # the record is kept
        assert payload[RUNTIME_VERSION_KEY] == "9.9.9", path  # the truth is added


def _rewrite(dir_, app, *, bump, **fields):
    """Rewrite a manifest and move its mtime, so the change is visible to stat."""
    _write(dir_, app, **fields)
    path = dir_ / f"{app}.json"
    st = path.stat()
    os.utime(path, (st.st_atime + bump, st.st_mtime + bump))


def test_per_app_meta_rereads_a_rewritten_manifest(single_app_dir, manifest_dir):
    """An app's manifest rewritten while the backend runs shows up at once (#81).

    Before, ``/api/foo/_meta`` and ``/foo/_meta`` served the compose-time
    snapshot until a restart, while the platform ``/_meta`` (#49) had already
    moved on: two endpoints disagreeing about the same app.
    """
    _write(manifest_dir, "foo", app_source={"sha": "old111"})
    config = discover_apps(
        PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    )
    client = TestClient(build_backend(config))
    for path in ("/api/foo/_meta", "/foo/_meta"):
        assert client.get(path).json()["app_source"]["sha"] == "old111"
    assert client.get("/api/foo/hello").headers["x-deploy-sha"] == "old111"

    _rewrite(manifest_dir, "foo", bump=2, app_source={"sha": "new222"})

    for path in ("/api/foo/_meta", "/foo/_meta"):
        payload = client.get(path).json()
        assert payload["app_source"]["sha"] == "new222", path
        on_disk = (manifest_dir / "foo.json").stat().st_mtime
        reported = datetime.fromisoformat(
            payload["manifest_mtime"].replace("Z", "+00:00")
        )
        assert abs(reported.timestamp() - on_disk) < 1
    # The response headers read through the same per-app reader.
    assert client.get("/api/foo/hello").headers["x-deploy-sha"] == "new222"


def test_per_app_meta_degrades_on_a_bad_rewrite_and_recovers(
    single_app_dir, manifest_dir
):
    _write(manifest_dir, "foo", app_source={"sha": "good1"})
    config = discover_apps(
        PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    )
    client = TestClient(build_backend(config))

    path = manifest_dir / "foo.json"
    path.write_text("{ not valid json")
    st = path.stat()
    os.utime(path, (st.st_atime + 2, st.st_mtime + 2))
    resp = client.get("/api/foo/_meta")
    assert resp.status_code == 200
    assert MANIFEST_ERROR_KEY in resp.json()["extra"]
    assert "x-deploy-sha" not in client.get("/api/foo/hello").headers

    _rewrite(manifest_dir, "foo", bump=4, app_source={"sha": "good2"})
    healthy = client.get("/api/foo/_meta").json()
    assert healthy["app_source"]["sha"] == "good2"
    assert MANIFEST_ERROR_KEY not in healthy["extra"]


def test_per_app_meta_picks_up_a_manifest_written_after_startup(
    single_app_dir, manifest_dir
):
    config = discover_apps(
        PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    )
    client = TestClient(build_backend(config))
    stub = client.get("/api/foo/_meta").json()
    assert stub["app_source"]["sha"] is None
    assert stub["manifest_mtime"] is None

    _write(manifest_dir, "foo", app_source={"sha": "late1"})
    assert client.get("/api/foo/_meta").json()["app_source"]["sha"] == "late1"


def test_injected_meta_tags_follow_a_rewritten_manifest(tmp_apps_dir, manifest_dir):
    frontend = tmp_apps_dir / "foo" / "frontend"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("<html><head></head><body>hi</body></html>")
    _write(manifest_dir, "foo", app_source={"sha": "aaa1111"})
    config = discover_apps(
        PlatformConfig(apps_dir=tmp_apps_dir, manifest_dir=manifest_dir)
    )
    client = TestClient(build_backend(config))
    assert 'content="aaa1111"' in client.get("/foo/").text

    _rewrite(manifest_dir, "foo", bump=2, app_source={"sha": "bbb2222"})
    assert 'content="bbb2222"' in client.get("/foo/").text


def test_a_deleted_app_manifest_reverts_to_the_stub(single_app_dir, manifest_dir):
    _write(manifest_dir, "foo", app_source={"sha": "gone111"})
    config = discover_apps(
        PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    )
    client = TestClient(build_backend(config))
    assert client.get("/api/foo/_meta").json()["app_source"]["sha"] == "gone111"

    (manifest_dir / "foo.json").unlink()
    payload = client.get("/api/foo/_meta").json()
    assert payload["app_source"]["sha"] is None
    assert payload["manifest_mtime"] is None


def test_a_same_tick_same_size_rewrite_is_not_missed(single_app_dir, manifest_dir):
    """mtime is coarse: a second write in the same tick keeps the stat key.

    Found by the review of #85. While the file is that young, it is re-read.
    """
    _write(manifest_dir, "foo", app_source={"sha": "aaaaaaa"})
    config = discover_apps(
        PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    )
    client = TestClient(build_backend(config))
    path = manifest_dir / "foo.json"
    first = path.stat()
    assert client.get("/api/foo/_meta").json()["app_source"]["sha"] == "aaaaaaa"

    with open(path, "r+") as f:  # in place: same inode, same size
        text = f.read().replace("aaaaaaa", "bbbbbbb")
        f.seek(0)
        f.write(text)
    os.utime(path, ns=(first.st_atime_ns, first.st_mtime_ns))  # same tick
    assert client.get("/api/foo/_meta").json()["app_source"]["sha"] == "bbbbbbb"


def test_a_manifest_that_breaks_the_parser_never_fails_a_request(
    single_app_dir, manifest_dir
):
    """``load_manifest`` only degrades on OSError/ValueError; the middlewares read
    the manifest on every request, so anything else must not escape (review of #85).
    """
    _write(manifest_dir, "foo", app_source={"sha": "good111"})
    config = discover_apps(
        PlatformConfig(apps_dir=single_app_dir, manifest_dir=manifest_dir)
    )
    client = TestClient(build_backend(config))
    assert client.get("/api/foo/hello").status_code == 200

    path = manifest_dir / "foo.json"
    path.write_text("[" * 200_000)  # RecursionError in json.load
    st = path.stat()
    os.utime(path, (st.st_atime + 2, st.st_mtime + 2))
    assert client.get("/api/foo/hello").status_code == 200
    assert client.get("/api/foo/_meta").json()["app_source"]["sha"] == "good111"
