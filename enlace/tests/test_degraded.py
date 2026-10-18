"""Loud and local: one app that cannot load gets a 500 stub, not the platform.

i2mint/enlace#37. Before it, one asgi app whose import raised killed every
gunicorn worker and every app on the platform answered 502. These tests pin the
replacement policy and, above all, that a stub is never silent: every stubbed
app is reported in /_meta, /_apps, ``enlace doctor`` and the logs.
"""

import importlib
import logging
import sys
import textwrap

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from enlace import doctor
from enlace.base import AppConfig, PlatformConfig
from enlace.compose import build_backend
from enlace.degraded import STUB_HEADER, STUB_HEADER_VALUE, EnlaceAppLoadError
from enlace.discover import discover_apps
from enlace.tests.conftest import (
    BROKEN_MODULE,
    PERMISSION_ERROR_MODULE,
    _make_app_code,
)


def _app(apps_dir, name, code, *, toml=None, frontend=False):
    d = apps_dir / name
    d.mkdir()
    (d / "server.py").write_text(code)
    if toml is not None:
        (d / "app.toml").write_text(toml)
    if frontend:
        (d / "frontend").mkdir()
        (d / "frontend" / "index.html").write_text(f"<html>{name} page</html>")
    return d


def _boot(apps_dir, **platform_kw):
    """Discover as the gateway does (recording import errors), then compose."""
    config = discover_apps(
        PlatformConfig(apps_dir=apps_dir, **platform_kw), on_import_error="record"
    )
    return config, TestClient(build_backend(config))


# A module that imports fine at discovery but fails when build_backend loads
# it: the configured app attribute does not exist.
NO_APP_ATTR = "x = 1\n"

# Imports fine the first time (discovery) and raises the second (build_backend
# re-imports): the shape of a flaky import, and the one way an app passes
# discovery yet fails to load at compose time.
SECOND_IMPORT_FAILS = textwrap.dedent("""\
    from pathlib import Path

    from fastapi import FastAPI

    _marker = Path(__file__).with_suffix(".imported")
    if _marker.exists():
        raise RuntimeError("fails on the second import")
    _marker.touch()
    app = FastAPI()
""")


# --- the gateway boots; only the broken app is affected ----------------------


def test_one_broken_app_among_several(multi_app_dir):
    _app(multi_app_dir, "broken", BROKEN_MODULE)

    _config, client = _boot(multi_app_dir)

    for name in ("alpha", "beta", "gamma"):
        r = client.get(f"/api/{name}/hello")
        assert r.status_code == 200
        assert r.json() == {"message": f"Hello from {name}"}

    for path in ("/api/broken/", "/api/broken/hello", "/api/broken/a/b?c=1"):
        r = client.get(path)
        assert r.status_code == 500
        assert "broken" in r.text and "ModuleNotFoundError" in r.text
        assert r.headers[STUB_HEADER] == STUB_HEADER_VALUE
        assert r.headers["cache-control"] == "no-store"
    assert client.post("/api/broken/x", json={}).status_code == 500


def test_stub_body_has_no_traceback_or_message(tmp_apps_dir):
    """The class is shown; the message (which can carry server paths) is not."""
    _app(tmp_apps_dir, "dotenv_reader", PERMISSION_ERROR_MODULE)

    _config, client = _boot(tmp_apps_dir)
    r = client.get("/api/dotenv_reader/")

    assert r.status_code == 500
    assert "PermissionError" in r.text
    assert "Traceback" not in r.text
    assert "/opt/somewhere" not in r.text
    assert "Permission denied" not in r.text
    meta = client.get("/_meta").text
    assert "/opt/somewhere" not in meta and "Permission denied" not in meta


def test_compose_time_failure_is_contained_too(tmp_apps_dir):
    """Discovery imported it fine; loading the sub-app is what fails."""
    _app(tmp_apps_dir, "ok", _make_app_code("ok"))
    _app(tmp_apps_dir, "flaky", SECOND_IMPORT_FAILS)

    config, client = _boot(tmp_apps_dir)

    assert client.get("/api/ok/hello").status_code == 200
    assert client.get("/api/flaky/").status_code == 500
    err = {a.name: a for a in config.apps}["flaky"].import_error
    assert (err.exception_type, err.stage) == ("RuntimeError", "compose")


def test_sys_exit_at_import_is_contained(tmp_apps_dir):
    """A module calling sys.exit() at import would otherwise end the worker."""
    _app(tmp_apps_dir, "ok", _make_app_code("ok"))
    _app(tmp_apps_dir, "quitter", "import sys\nsys.exit(3)\n")

    _config, client = _boot(tmp_apps_dir)

    assert client.get("/api/ok/hello").status_code == 200
    assert "SystemExit" in client.get("/api/quitter/").text


def test_broken_landing_app(tmp_apps_dir):
    """The landing app's backend breaks: / still serves its page, others serve."""
    _app(tmp_apps_dir, "landing", BROKEN_MODULE, frontend=True)
    _app(tmp_apps_dir, "ok", _make_app_code("ok"))

    _config, client = _boot(tmp_apps_dir, landing_app="landing")

    assert "landing page" in client.get("/").text
    assert client.get("/api/ok/hello").status_code == 200
    assert client.get("/api/landing/").status_code == 500
    assert "landing" in client.get("/_meta").json()["degraded_apps"]


def test_broken_process_mode_app_is_unaffected(tmp_apps_dir):
    """A process-mode app is never imported, so its broken server.py is moot."""
    _app(
        tmp_apps_dir,
        "worker",
        BROKEN_MODULE,
        toml='mode = "process"\ncommand = ["node", "server.js"]\nport = 3001\n',
    )
    _app(tmp_apps_dir, "ok", _make_app_code("ok"))

    config, client = _boot(tmp_apps_dir)

    assert [a.import_error for a in config.apps] == [None, None]
    assert "degraded_apps" not in client.get("/_meta").json()


def test_a_stub_does_not_shadow_a_neighbouring_prefix(tmp_apps_dir):
    """Mount matching is per path segment: /api/foo's stub is not /api/foobar."""
    _app(tmp_apps_dir, "foo", BROKEN_MODULE)
    _app(tmp_apps_dir, "foobar", _make_app_code("foobar"))

    _config, client = _boot(tmp_apps_dir)

    assert client.get("/api/foobar/hello").status_code == 200
    assert client.get("/api/foo/hello").status_code == 500


def test_a_stub_stays_behind_the_platform_auth_gate(tmp_apps_dir):
    """Parent middleware (where enlace_auth gates) runs before the stub."""

    def gate(parent: FastAPI, config) -> None:
        class Gate:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http" and scope["path"].startswith("/api/secret"):
                    await send({"type": "http.response.start", "status": 401})
                    await send({"type": "http.response.body", "body": b"no"})
                    return
                await self.app(scope, receive, send)

        parent.add_middleware(Gate)

    _app(tmp_apps_dir, "secret", BROKEN_MODULE)
    config = discover_apps(
        PlatformConfig(apps_dir=tmp_apps_dir), on_import_error="record"
    )
    client = TestClient(build_backend(config, plugins=[gate]))

    r = client.get("/api/secret/anything")
    assert r.status_code == 401
    assert "ModuleNotFoundError" not in r.text


def test_lifespan_runs_with_a_stub_mounted(multi_app_dir):
    """The stub has no router, so the lifespan cascade passes it by."""
    _app(multi_app_dir, "broken", BROKEN_MODULE)
    config = discover_apps(
        PlatformConfig(apps_dir=multi_app_dir), on_import_error="record"
    )
    with TestClient(build_backend(config)) as client:
        assert client.get("/api/alpha/hello").status_code == 200
        assert client.get("/api/broken/").status_code == 500


def test_websocket_to_a_stub_is_refused(tmp_apps_dir):
    from starlette.websockets import WebSocketDisconnect

    _app(tmp_apps_dir, "broken", BROKEN_MODULE)
    _config, client = _boot(tmp_apps_dir)

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/api/broken/ws"):
            pass
    assert excinfo.value.code == 1011


def test_partial_imports_are_forgotten(tmp_apps_dir):
    """Modules of the app's own package loaded before the failure are dropped."""
    d = _app(
        tmp_apps_dir,
        "halfway",
        "from apps.halfway import helpers\nimport nonexistent_package_xyz\n",
    )
    (d / "helpers.py").write_text("LOADED = True\n")

    config, _client = _boot(tmp_apps_dir)

    # helpers really did load: the failure is the later import, not this one
    assert "nonexistent_package_xyz" in config.apps[0].import_error.message
    assert not [m for m in sys.modules if m.startswith("apps.halfway.")]
    # ...and not left bound on the parent package for `from ... import` to find
    assert not hasattr(sys.modules.get("apps.halfway"), "helpers")


# --- no stub is ever silent ---------------------------------------------------


@pytest.fixture(params=["discover", "compose"])
def platform_with_a_stub(request, tmp_apps_dir):
    """A booted platform with one stubbed app, broken at either stage."""
    _app(tmp_apps_dir, "ok", _make_app_code("ok"), frontend=True)
    if request.param == "discover":
        _app(tmp_apps_dir, "broken", BROKEN_MODULE, frontend=True)
        config, client = _boot(tmp_apps_dir)
    else:
        # Imports fine at discovery, fails when build_backend loads it.
        _app(tmp_apps_dir, "broken", SECOND_IMPORT_FAILS, frontend=True)
        config, client = _boot(tmp_apps_dir)
    return request.param, config, client


def test_no_stub_is_ever_silent(platform_with_a_stub, monkeypatch):
    stage, config, client = platform_with_a_stub

    # /_meta, platform and per-app, both mounts
    meta = client.get("/_meta")
    assert meta.status_code == 200
    assert meta.json()["degraded_apps"]["broken"]["stage"] == stage
    for path in ("/api/broken/_meta", "/broken/_meta"):
        assert client.get(path).json()["degraded"]["stage"] == stage
    assert "degraded" not in client.get("/api/ok/_meta").json()

    # /_apps: listed as broken, not launchable
    items = {i["name"]: i for i in client.get("/_apps").json()["apps"]}
    assert items["broken"]["degraded"] is True
    assert items["broken"]["launchable"] is False
    assert items["broken"]["launch_url"] is None
    assert items["ok"]["degraded"] is False and items["ok"]["launchable"] is True

    # doctor, static: FAIL from the config the gateway built
    static = doctor.run_doctor(config)
    assert [c.status for c in static.checks if c.name == "import:broken"] == ["fail"]

    # doctor, live: FAIL from the gateway's own /_meta
    def via_client(url, *, timeout=None):
        r = client.get(url.replace("http://gw", ""))
        return r.status_code, dict(r.headers), r.content, None

    monkeypatch.setattr(doctor, "_http_get", via_client)
    live = doctor.run_doctor(config, base_url="http://gw")
    by_name = {c.name: c for c in live.checks}
    assert by_name["degraded:broken"].status == "fail"
    assert by_name["http:/api/broken/"].status == "fail"
    assert "meta:degraded_apps" not in by_name  # no PASS alongside a FAIL


def test_a_stub_is_logged_with_its_traceback(tmp_apps_dir, caplog):
    _app(tmp_apps_dir, "broken", BROKEN_MODULE)
    with caplog.at_level(logging.ERROR, logger="enlace"):
        _boot(tmp_apps_dir)
    [record] = [r for r in caplog.records if "failed to load" in r.getMessage()]
    assert record.levelno == logging.ERROR
    assert "'broken'" in record.getMessage()
    assert record.exc_info and record.exc_info[0] is ModuleNotFoundError


def test_a_healthy_platform_stays_quiet(multi_app_dir, monkeypatch):
    config, client = _boot(multi_app_dir)
    assert "degraded_apps" not in client.get("/_meta").json()

    def via_client(url, *, timeout=None):
        r = client.get(url.replace("http://gw", ""))
        return r.status_code, dict(r.headers), r.content, None

    monkeypatch.setattr(doctor, "_http_get", via_client)
    checks = {c.name: c for c in doctor.run_doctor(config, base_url="http://gw").checks}
    assert checks["meta:degraded_apps"].status == "pass"


# --- what still refuses to boot -----------------------------------------------


def test_fail_hard_app_refuses_to_boot(tmp_apps_dir):
    _app(tmp_apps_dir, "ok", _make_app_code("ok"))
    _app(tmp_apps_dir, "strict", BROKEN_MODULE, toml="fail_hard = true\n")
    config = discover_apps(
        PlatformConfig(apps_dir=tmp_apps_dir), on_import_error="record"
    )
    with pytest.raises(ModuleNotFoundError, match="nonexistent_package_xyz"):
        build_backend(config)


def test_platform_raise_policy_refuses_to_boot(tmp_apps_dir):
    _app(tmp_apps_dir, "broken", BROKEN_MODULE)
    config = discover_apps(
        PlatformConfig(apps_dir=tmp_apps_dir, app_load_failure="raise"),
        on_import_error="record",
    )
    with pytest.raises(ModuleNotFoundError):
        build_backend(config)


def test_raise_policy_applies_to_compose_time_failures(tmp_apps_dir):
    d = _app(tmp_apps_dir, "noattr", NO_APP_ATTR)
    app = AppConfig(
        name="noattr",
        route_prefix="/api/noattr",
        entry_module_path=d / "server.py",
        app_type="asgi_app",
        source_dir=tmp_apps_dir,
        fail_hard=True,
    )
    with pytest.raises(AttributeError):
        build_backend(PlatformConfig(apps=[app]))


def test_raise_without_the_original_exception(tmp_apps_dir):
    from enlace.base import AppImportError

    app = AppConfig(
        name="x",
        route_prefix="/api/x",
        app_type="asgi_app",
        fail_hard=True,
        import_error=AppImportError(exception_type="ValueError", message="m"),
    )
    with pytest.raises(EnlaceAppLoadError, match="ValueError: m"):
        build_backend(PlatformConfig(apps=[app]))


def test_platform_config_errors_still_refuse_to_boot(tmp_apps_dir):
    """Only per-app loads are contained; platform configuration is not."""
    _app(tmp_apps_dir, "broken", BROKEN_MODULE)
    config = discover_apps(
        PlatformConfig(apps_dir=tmp_apps_dir, landing_app="nope"),
        on_import_error="record",
    )
    with pytest.raises(ValueError, match="landing_app"):
        build_backend(config)

    def refusing_plugin(parent, cfg):
        raise RuntimeError("auth allowlist is empty")

    config = discover_apps(
        PlatformConfig(apps_dir=tmp_apps_dir), on_import_error="record"
    )
    with pytest.raises(RuntimeError, match="allowlist"):
        build_backend(config, plugins=[refusing_plugin])


def test_route_conflicts_still_refuse_to_boot(tmp_apps_dir):
    _app(tmp_apps_dir, "one", BROKEN_MODULE, toml='route = "/api/same"\n')
    _app(tmp_apps_dir, "two", _make_app_code("two"), toml='route = "/api/same"\n')
    with pytest.raises(RuntimeError, match="conflict"):
        discover_apps(PlatformConfig(apps_dir=tmp_apps_dir), on_import_error="record")


def test_a_non_asgi_mode_load_error_is_not_contained(tmp_apps_dir):
    """Another mode's make_asgi failure is configuration, and propagates."""
    from enlace.strategies import _STRATEGIES, BackendStrategy, register_strategy

    class Exploding(BackendStrategy):
        name = "exploding-test-mode"

        def make_asgi(self, app, platform):
            raise ImportError("plugin dependency missing")

    register_strategy(Exploding())
    try:
        app = AppConfig(
            name="x", route_prefix="/api/x", app_type="asgi_app", mode=Exploding.name
        )
        with pytest.raises(ImportError, match="plugin dependency"):
            build_backend(PlatformConfig(apps=[app]))
    finally:
        _STRATEGIES.pop(Exploding.name, None)


def test_a_bad_plugin_spec_still_refuses_to_boot(tmp_path, monkeypatch):
    from enlace.compose import EnlaceConfigError, create_app

    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    _app(apps_dir, "broken", BROKEN_MODULE)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENLACE_APPS_DIRS", str(apps_dir))
    monkeypatch.setenv("ENLACE_PLUGINS", "no_such_plugin_module_xyz:plugin")
    with pytest.raises(EnlaceConfigError):
        create_app()


def test_fail_hard_is_read_from_app_toml(tmp_apps_dir):
    _app(tmp_apps_dir, "strict", _make_app_code("s"), toml="fail_hard = true\n")
    [app] = discover_apps(PlatformConfig(apps_dir=tmp_apps_dir)).apps
    assert app.fail_hard is True
    assert app.provenance["fail_hard"] == "override: app.toml"


# --- adversarial-review regressions -------------------------------------------


def test_nothing_to_mount_fails_at_discovery_not_only_at_boot(tmp_apps_dir):
    """No `app` and no public function: `enlace check` and the static doctor
    must see it, not only the booted gateway (review finding 1)."""
    _app(tmp_apps_dir, "noattr", NO_APP_ATTR)
    config = discover_apps(
        PlatformConfig(apps_dir=tmp_apps_dir), on_import_error="record"
    )
    [check] = [c for c in doctor.run_doctor(config).checks if c.status == "fail"]
    assert check.name == "import:noattr"
    assert check.extra["exception_type"] == "AttributeError"


def test_app_toml_app_attr_is_used_for_detection(tmp_apps_dir):
    code = "from fastapi import FastAPI\napplication = FastAPI()\n"
    _app(tmp_apps_dir, "custom", code, toml='app_attr = "application"\n')
    config, client = _boot(tmp_apps_dir)
    assert config.apps[0].import_error is None
    assert "degraded_apps" not in client.get("/_meta").json()


def test_app_toml_entry_point_is_used_for_detection(tmp_apps_dir):
    """A stale broken server.py beside the declared entry must not stub the app."""
    d = _app(tmp_apps_dir, "moved", BROKEN_MODULE, toml='entry_point = "main2.py"\n')
    (d / "main2.py").write_text(_make_app_code("moved"))
    config, client = _boot(tmp_apps_dir)
    assert config.apps[0].import_error is None
    assert client.get("/api/moved/hello").status_code == 200


def test_serve_refuses_before_spawning_anything(tmp_apps_dir, monkeypatch):
    """fail_hard stops `enlace serve` up front, as discovery used to (finding 2)."""
    serve_mod = importlib.import_module("enlace.serve")

    for var in ("ENLACE_APPS_DIRS", "ENLACE_APP_DIRS", "ENLACE_APPS_DIR"):
        monkeypatch.setenv(var, "")  # serve() writes these; restore after

    def must_not_run(*a, **k):
        raise AssertionError("serve spawned a server for a fail_hard app")

    monkeypatch.setattr(serve_mod, "_serve_asgi_only", must_not_run)
    monkeypatch.setattr(serve_mod, "_serve_mixed", must_not_run)
    _app(tmp_apps_dir, "strict", BROKEN_MODULE, toml="fail_hard = true\n")
    monkeypatch.chdir(tmp_apps_dir.parent)

    with pytest.raises(ModuleNotFoundError):
        serve_mod.serve(apps_dir=str(tmp_apps_dir))


# --- doctor reads the live /_meta (#78, and the #77 review of a blind read) ----


def _doctor_against(client, monkeypatch, base="http://gw"):
    def via_client(url, *, timeout=None):
        r = client.get(url.replace(base, ""))
        return r.status_code, dict(r.headers), r.content, None

    monkeypatch.setattr(doctor, "_http_get", via_client)
    return {c.name: c for c in doctor._check_live_meta(base, 1.0)}


def test_a_blind_meta_read_is_a_fail_not_a_warn(monkeypatch):
    """The only detector of a stub behind an auth gate must not go quietly blind:
    a deploy gate that sets aside the doctor-local import checks would otherwise
    pass with nothing examined about stubs."""
    for status, body, err in [
        (200, b"<html>landing</html>", None),
        (502, b"", None),
        (None, None, "connection failed: refused"),
    ]:
        monkeypatch.setattr(
            doctor, "_http_get", lambda url, *, timeout=None: (status, {}, body, err)
        )
        [check] = doctor._check_live_meta("http://gw", 1.0)
        assert (check.name, check.status) == ("meta:degraded_apps", "fail")


def test_doctor_warns_when_the_engine_was_upgraded_since_the_deploy(
    multi_app_dir, monkeypatch, tmp_path
):
    from enlace import compose

    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "_platform.json").write_text(
        '{"app": "_platform", "enlace_version": "0.0.1"}'
    )
    monkeypatch.setattr(compose, "_detect_enlace_version", lambda: "9.9.9")
    _config, client = _boot(multi_app_dir, manifest_dir=manifests)

    checks = _doctor_against(client, monkeypatch)
    assert checks["meta:enlace_version"].status == "warn"
    assert "9.9.9" in checks["meta:enlace_version"].detail
    assert checks["meta:degraded_apps"].status == "pass"


def test_no_version_warning_when_they_agree(multi_app_dir, monkeypatch, tmp_path):
    from enlace import compose

    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "_platform.json").write_text(
        '{"app": "_platform", "enlace_version": "9.9.9"}'
    )
    monkeypatch.setattr(compose, "_detect_enlace_version", lambda: "9.9.9")
    _config, client = _boot(multi_app_dir, manifest_dir=manifests)
    assert "meta:enlace_version" not in _doctor_against(client, monkeypatch)
