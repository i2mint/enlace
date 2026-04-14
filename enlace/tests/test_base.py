"""Tests for config model validation — especially the new mode field."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from enlace.base import AppConfig, PlatformConfig

# -- Helpers ------------------------------------------------------------------

def _asgi_app(**overrides):
    """Build a minimal valid asgi-mode AppConfig."""
    defaults = dict(
        name="foo",
        route_prefix="/api/foo",
        app_type="asgi_app",
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


def _process_app(**overrides):
    """Build a minimal valid process-mode AppConfig."""
    defaults = dict(
        name="bar",
        route_prefix="/api/bar",
        app_type="asgi_app",
        mode="process",
        command=["node", "server.js"],
        port=9100,
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


def _external_app(**overrides):
    """Build a minimal valid external-mode AppConfig."""
    defaults = dict(
        name="ext",
        route_prefix="/api/ext",
        app_type="asgi_app",
        mode="external",
        upstream_url="http://192.168.1.50:3000",
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


def _static_app(**overrides):
    """Build a minimal valid static-mode AppConfig."""
    defaults = dict(
        name="docs",
        route_prefix="/docs",
        app_type="frontend_only",
        mode="static",
        public_dir=Path("dist"),
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


# -- Mode defaults & backward compat ------------------------------------------

def test_default_mode_is_asgi():
    app = _asgi_app()
    assert app.mode == "asgi"


def test_existing_fields_unchanged():
    """Existing configs with no mode field work identically."""
    app = _asgi_app(entry_module_path=Path("server.py"), access="public")
    assert app.mode == "asgi"
    assert app.entry_module_path == Path("server.py")
    assert app.access == "public"
    assert app.display_name == "Foo"


# -- Process mode validation ---------------------------------------------------

def test_process_mode_valid():
    app = _process_app()
    assert app.mode == "process"
    assert app.command == ["node", "server.js"]
    assert app.port == 9100


def test_process_mode_with_socket():
    app = _process_app(port=None, socket="/tmp/enlace/bar.sock")
    assert app.socket == "/tmp/enlace/bar.sock"
    assert app.port is None


def test_process_mode_requires_command():
    with pytest.raises(ValidationError, match="requires 'command'"):
        _process_app(command=None)


def test_process_mode_empty_command_rejected():
    with pytest.raises(ValidationError, match="requires 'command'"):
        _process_app(command=[])


def test_process_mode_requires_port_or_socket():
    with pytest.raises(ValidationError, match="requires 'port' or 'socket'"):
        _process_app(port=None, socket=None)


def test_process_mode_rejects_both_port_and_socket():
    with pytest.raises(ValidationError, match="not both"):
        _process_app(port=9100, socket="/tmp/enlace/bar.sock")


def test_process_mode_default_fields():
    app = _process_app()
    assert app.health_check_path == "/health"
    assert app.ready_timeout == 30.0
    assert app.restart_policy == "on-failure"
    assert app.max_retries == 5
    assert app.restart_delay_ms == 100
    assert app.env == {}
    assert app.build is None


# -- External mode validation --------------------------------------------------

def test_external_mode_valid():
    app = _external_app()
    assert app.mode == "external"
    assert app.upstream_url == "http://192.168.1.50:3000"


def test_external_mode_requires_upstream_url():
    with pytest.raises(ValidationError, match="requires 'upstream_url'"):
        _external_app(upstream_url=None)


# -- Static mode validation ----------------------------------------------------

def test_static_mode_valid_with_public_dir():
    app = _static_app()
    assert app.mode == "static"
    assert app.public_dir == Path("dist")


def test_static_mode_valid_with_frontend_dir():
    app = _static_app(public_dir=None, frontend_dir=Path("frontend"))
    assert app.mode == "static"
    assert app.frontend_dir == Path("frontend")


def test_static_mode_requires_dir():
    with pytest.raises(ValidationError, match="requires"):
        _static_app(public_dir=None, frontend_dir=None)


# -- PlatformConfig extensions -------------------------------------------------

def test_platform_config_new_defaults():
    config = PlatformConfig()
    assert config.process_port_start == 9100
    assert config.socket_dir == Path("/tmp/enlace")


def test_platform_config_port_conflict_detection():
    apps = [
        _process_app(name="a", route_prefix="/api/a"),
        _process_app(name="b", route_prefix="/api/b", port=9100),
    ]
    config = PlatformConfig(apps=apps)
    errors = config.check_conflicts()
    assert any("Port conflict" in e and "9100" in e for e in errors)


def test_platform_config_no_port_conflict_different_ports():
    apps = [
        _process_app(name="a", route_prefix="/api/a", port=9100),
        _process_app(name="b", route_prefix="/api/b", port=9101),
    ]
    config = PlatformConfig(apps=apps)
    errors = config.check_conflicts()
    assert not errors


def test_platform_config_port_conflict_ignores_asgi_apps():
    """asgi-mode apps don't have ports, so no conflict even if port field is None."""
    apps = [
        _asgi_app(name="a", route_prefix="/api/a"),
        _asgi_app(name="b", route_prefix="/api/b"),
    ]
    config = PlatformConfig(apps=apps)
    errors = config.check_conflicts()
    assert not errors
