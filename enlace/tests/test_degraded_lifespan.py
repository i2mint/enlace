"""Lifespan startup failures are loud and local too (i2mint/enlace#76).

Two things are pinned here. First, mounted apps' lifespans actually run: app
mounts are ``_AppIdInjector`` wrappers, and until #76 the cascade looked for a
``router`` on the wrapper, found none, and silently ran no app's startup or
shutdown at all. Second, an asgi app whose startup raises is swapped for the
500 stub and reported like an import failure, while every other app starts,
serves, and shuts down.
"""

import textwrap

import pytest
from fastapi.testclient import TestClient

from enlace.base import AppConfig, PlatformConfig
from enlace.compose import build_backend
from enlace.discover import discover_apps


def _lifespan_app(apps_dir, name, log, *, fail=False, toml=None):
    d = apps_dir / name
    d.mkdir()
    (d / "server.py").write_text(
        textwrap.dedent(f"""\
            from contextlib import asynccontextmanager
            from pathlib import Path

            from fastapi import FastAPI

            _log = Path({str(log)!r})

            @asynccontextmanager
            async def lifespan(app):
                with _log.open("a") as f:
                    f.write("start {name}\\n")
                if {fail!r}:
                    raise RuntimeError("startup of {name} failed")
                yield
                with _log.open("a") as f:
                    f.write("stop {name}\\n")

            app = FastAPI(lifespan=lifespan)

            @app.get("/hello")
            def hello():
                return {{"app": "{name}"}}
        """)
    )
    if toml is not None:
        (d / "app.toml").write_text(toml)


def _events(log):
    return log.read_text().splitlines() if log.exists() else []


def test_mounted_apps_lifespans_run(tmp_path):
    """Before #76 no app's lifespan ran: the mount wrapper hid its router."""
    apps, log = tmp_path / "apps", tmp_path / "log"
    apps.mkdir()
    _lifespan_app(apps, "alpha", log)

    config = discover_apps(PlatformConfig(apps_dir=apps))
    with TestClient(build_backend(config)) as client:
        assert _events(log) == ["start alpha"]
        assert client.get("/api/alpha/hello").json() == {"app": "alpha"}
    assert _events(log) == ["start alpha", "stop alpha"]


@pytest.mark.parametrize("broken", ["a_first", "m_middle", "z_last"])
def test_a_failing_startup_stubs_that_app_only(tmp_path, broken):
    apps, log = tmp_path / "apps", tmp_path / "log"
    apps.mkdir()
    names = ["a_first", "m_middle", "z_last"]
    for name in names:
        _lifespan_app(apps, name, log, fail=(name == broken))

    config = discover_apps(PlatformConfig(apps_dir=apps))
    with TestClient(build_backend(config)) as client:
        for name in names:
            r = client.get(f"/api/{name}/hello")
            if name == broken:
                assert r.status_code == 500
                assert "RuntimeError" in r.text
                assert "startup of" not in r.text  # the class, never the message
            else:
                assert r.json() == {"app": name}

        meta = client.get("/_meta").json()
        assert meta["degraded_apps"] == {
            broken: {"exception_type": "RuntimeError", "stage": "lifespan"}
        }
        assert client.get(f"/api/{broken}/_meta").json()["degraded"]["stage"] == (
            "lifespan"
        )
        items = {i["name"]: i for i in client.get("/_apps").json()["apps"]}
        assert items[broken]["degraded"] is True
        assert items[broken]["launchable"] is False

    started = [n for n in names if n != broken]
    # Every app was attempted; the ones that started are the ones shut down.
    assert sorted(e for e in _events(log) if e.startswith("start")) == sorted(
        f"start {n}" for n in names
    )
    assert sorted(e for e in _events(log) if e.startswith("stop")) == sorted(
        f"stop {n}" for n in started
    )


def test_a_failing_startup_is_logged_with_its_traceback(tmp_path, caplog):
    import logging

    apps, log = tmp_path / "apps", tmp_path / "log"
    apps.mkdir()
    _lifespan_app(apps, "boom", log, fail=True)

    config = discover_apps(PlatformConfig(apps_dir=apps))
    with caplog.at_level(logging.ERROR, logger="enlace"):
        with TestClient(build_backend(config)):
            pass
    [record] = [r for r in caplog.records if "failed to load" in r.getMessage()]
    assert "lifespan" in record.getMessage()
    assert record.exc_info and record.exc_info[0] is RuntimeError


def test_fail_hard_aborts_startup(tmp_path):
    apps, log = tmp_path / "apps", tmp_path / "log"
    apps.mkdir()
    _lifespan_app(apps, "ok", log)
    _lifespan_app(apps, "strict", log, fail=True, toml="fail_hard = true\n")

    config = discover_apps(PlatformConfig(apps_dir=apps))
    with pytest.raises(RuntimeError, match="startup of strict failed"):
        with TestClient(build_backend(config)):
            pass


def test_platform_raise_policy_aborts_startup(tmp_path):
    apps, log = tmp_path / "apps", tmp_path / "log"
    apps.mkdir()
    _lifespan_app(apps, "boom", log, fail=True)

    config = discover_apps(PlatformConfig(apps_dir=apps, app_load_failure="raise"))
    with pytest.raises(RuntimeError, match="startup of boom failed"):
        with TestClient(build_backend(config)):
            pass


def test_a_non_asgi_mode_startup_failure_is_not_contained():
    """Another mode's sub-app failing at startup is configuration: it aborts."""
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from enlace.strategies import _STRATEGIES, BackendStrategy, register_strategy

    @asynccontextmanager
    async def lifespan(app):
        raise RuntimeError("plugin backend could not start")
        yield

    class Failing(BackendStrategy):
        name = "failing-lifespan-test-mode"

        def make_asgi(self, app, platform):
            return FastAPI(lifespan=lifespan)

    register_strategy(Failing())
    try:
        app = AppConfig(
            name="x", route_prefix="/api/x", app_type="asgi_app", mode=Failing.name
        )
        with pytest.raises(RuntimeError, match="plugin backend"):
            with TestClient(build_backend(PlatformConfig(apps=[app]))):
                pass
    finally:
        _STRATEGIES.pop(Failing.name, None)
