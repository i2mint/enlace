"""Tests for enlace.doctor — plugin check discovery and static checks."""

# ---------------------------------------------------------------------- #
# Plugin check discovery
#
# A plugin can ship a correct diagnosis of its own failure mode and have it
# never execute: `extra_static_checks` was hand-wiring nobody did. Discovery
# ties the checks that RUN to the plugins that RUN.
# ---------------------------------------------------------------------- #

import sys
import types

from enlace.base import AppConfig, AppImportError, PlatformConfig
from enlace.doctor import FAIL, discover_plugin_checks, run_doctor


def _fake_plugin(monkeypatch, name, *, static=(), http=(), broken=False):
    pkg = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, pkg)
    if broken:
        return
    diag = types.ModuleType(f"{name}.diagnostics")
    diag.static_checks = static
    diag.http_checks = http
    monkeypatch.setitem(sys.modules, f"{name}.diagnostics", diag)


def test_discovers_checks_from_a_configured_plugin(monkeypatch):
    def a(config):
        return []

    def b(config, base_url, timeout):
        return []

    _fake_plugin(monkeypatch, "fakeplug", static=(a,), http=(b,))
    static, http = discover_plugin_checks("fakeplug:plugin")
    assert static == (a,) and http == (b,)


def test_reads_ENLACE_PLUGINS_by_default(monkeypatch):
    def a(config):
        return []

    _fake_plugin(monkeypatch, "envplug", static=(a,))
    monkeypatch.setenv("ENLACE_PLUGINS", "envplug:plugin")
    static, _ = discover_plugin_checks()
    assert static == (a,)


def test_a_plugin_without_diagnostics_contributes_nothing(monkeypatch):
    _fake_plugin(monkeypatch, "plainplug", broken=True)
    assert discover_plugin_checks("plainplug:plugin") == ((), ())


def test_an_unimportable_plugin_never_breaks_the_doctor():
    """Doctor reports problems; it must not become one."""
    assert discover_plugin_checks("no_such_module_at_all:plugin") == ((), ())


def test_each_package_is_collected_once(monkeypatch):
    def a(config):
        return []

    _fake_plugin(monkeypatch, "dupplug", static=(a,))
    static, _ = discover_plugin_checks("dupplug:plugin,dupplug:other")
    assert static == (a,)


def test_empty_and_blank_specs_are_harmless():
    assert discover_plugin_checks("") == ((), ())
    assert discover_plugin_checks("  ,  ") == ((), ())


# ---------------------------------------------------------------------- #
# Un-importable apps
#
# The doctor used to die on the failure it exists to detect: discovery
# imports every asgi-mode app, so one broken app meant no report at all.
# ---------------------------------------------------------------------- #


def _config_with_import_error(**err_kwargs) -> PlatformConfig:
    """A config as `discover_apps(..., on_import_error="record")` would build it."""
    return PlatformConfig(
        apps=[
            AppConfig(name="healthy", route_prefix="/api/healthy", app_type="asgi_app"),
            AppConfig(
                name="broken",
                route_prefix="/api/broken",
                app_type="asgi_app",
                import_error=AppImportError(**err_kwargs),
            ),
        ]
    )


def test_an_unimportable_app_is_a_FAIL_naming_the_exception():
    config = _config_with_import_error(
        exception_type="ModuleNotFoundError",
        message="No module named 'nonexistent_package_xyz'",
        entry_module_path="/apps/broken/server.py",
    )
    report = run_doctor(config)

    assert report.ok is False
    check = next(c for c in report.checks if c.name == "import:broken")
    assert check.status == FAIL
    assert "ModuleNotFoundError" in check.detail
    assert check.extra["app"] == "broken"
    assert check.extra["exception_type"] == "ModuleNotFoundError"


def test_a_non_ImportError_is_reported_the_same_way():
    """The production case was a PermissionError, not an ImportError."""
    config = _config_with_import_error(
        exception_type="PermissionError",
        message="[Errno 13] Permission denied: '/opt/somewhere/.env'",
    )
    check = next(c for c in run_doctor(config).checks if c.name == "import:broken")
    assert check.extra["exception_type"] == "PermissionError"


def test_healthy_apps_produce_no_import_checks():
    config = PlatformConfig(
        apps=[
            AppConfig(name="healthy", route_prefix="/api/healthy", app_type="asgi_app")
        ]
    )
    report = run_doctor(config)
    assert [c for c in report.checks if c.name.startswith("import:")] == []
    assert report.ok is True
