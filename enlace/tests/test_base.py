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


def test_extra_fields_preserved_for_plugin_strategies():
    """AppConfig accepts and preserves unknown fields so plugin BackendStrategies
    can carry their own typed fields (e.g. ``dockerfile``, ``image``) without
    enlace having to know about them."""
    app = _asgi_app(dockerfile="Dockerfile", build_args={"X": "y"})
    assert app.dockerfile == "Dockerfile"
    assert app.build_args == {"X": "y"}


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


# -- from_toml: host-portable relative path resolution ------------------------


def _write_toml(path: Path, body: str) -> Path:
    """Write a platform.toml (body goes under the [platform] table)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[platform]\n" + body)
    return path


def test_from_toml_resolves_relative_apps_dirs_against_toml_dir(tmp_path, monkeypatch):
    """Relative apps_dirs resolve against the toml's directory, not the CWD."""
    # Layout: <root>/cfg/platform.toml points at <root>/papp via "../papp".
    (tmp_path / "papp").mkdir()
    toml = _write_toml(
        tmp_path / "cfg" / "platform.toml",
        'apps_dirs = ["../papp"]\n',
    )
    # Run from an unrelated CWD to prove resolution is toml-relative.
    monkeypatch.chdir(tmp_path / "papp")

    config = PlatformConfig.from_toml(toml)

    assert config.apps_dirs == [(tmp_path / "papp").resolve()]


def test_from_toml_resolves_relative_app_dirs_and_shared_assets(tmp_path, monkeypatch):
    """app_dirs and shared_assets_dir also resolve against the toml dir."""
    (tmp_path / "reelee-web").mkdir()
    (tmp_path / "cfg" / "static").mkdir(parents=True)
    toml = _write_toml(
        tmp_path / "cfg" / "platform.toml",
        'app_dirs = ["../reelee-web"]\nshared_assets_dir = "static"\n',
    )
    monkeypatch.chdir(tmp_path)

    config = PlatformConfig.from_toml(toml)

    assert config.app_dirs == [(tmp_path / "reelee-web").resolve()]
    assert config.shared_assets_dir == (tmp_path / "cfg" / "static").resolve()


def test_from_toml_leaves_absolute_paths_untouched(tmp_path):
    """Absolute paths are passed through unchanged."""
    abs_apps = tmp_path / "elsewhere" / "apps"
    abs_apps.mkdir(parents=True)
    toml = _write_toml(
        tmp_path / "cfg" / "platform.toml",
        f'apps_dirs = ["{abs_apps}"]\n',
    )

    config = PlatformConfig.from_toml(toml)

    assert config.apps_dirs == [abs_apps]


def test_from_toml_env_override_is_verbatim(tmp_path, monkeypatch):
    """ENLACE_APPS_DIRS overrides toml values and is taken verbatim."""
    toml = _write_toml(
        tmp_path / "cfg" / "platform.toml",
        'apps_dirs = ["../papp"]\n',
    )
    monkeypatch.setenv("ENLACE_APPS_DIRS", "/opt/tw_platform/apps")

    config = PlatformConfig.from_toml(toml)

    assert config.apps_dirs == [Path("/opt/tw_platform/apps")]


def test_from_toml_missing_file_returns_defaults(tmp_path):
    """A non-existent toml path yields defaults without crashing."""
    config = PlatformConfig.from_toml(tmp_path / "does-not-exist.toml")

    assert config.apps_dirs == [Path("apps")]
    assert config.app_dirs == []


# -- from_toml: unknown top-level tables (#63) --------------------------------


def test_from_toml_warns_on_unknown_top_level_tables(tmp_path, caplog):
    """A typo or misnested table is reported, not silently dropped."""
    toml = tmp_path / "platform.toml"
    toml.write_text(
        "[platform]\n\n[stores_]\nx = 1\n\n[oauth_server]\nenabled = true\n"
    )
    with caplog.at_level("WARNING", logger="enlace.config"):
        config = PlatformConfig.from_toml(toml)

    assert config.unknown_tables == [
        "[stores_] is not an enlace table and is ignored (did you mean [stores]?)",
        "[oauth_server] is not an enlace table and is ignored",
    ]
    logged = caplog.text
    assert "[stores_]" in logged and "[oauth_server]" in logged
    assert "foreign_tables" in logged


def test_from_toml_foreign_tables_silences_tables_owned_by_other_tools(
    tmp_path, caplog
):
    """Tables declared in [platform] foreign_tables load quietly."""
    toml = tmp_path / "platform.toml"
    toml.write_text(
        '[platform]\nforeign_tables = ["deploy", "google_analytics"]\n\n'
        '[deploy]\nexternal_apps = ["a"]\n\n[google_analytics]\nga_id = "G-X"\n'
        "\n[app_meta]\n\n[analytics]\n\n[auth]\n\n[stores.s]\n\n[conventions]\n"
    )
    with caplog.at_level("WARNING", logger="enlace.config"):
        config = PlatformConfig.from_toml(toml)

    assert config.unknown_tables == []
    assert config.foreign_tables == ["deploy", "google_analytics"]
    assert "not an enlace table" not in caplog.text


def test_unknown_tables_survive_model_copy(tmp_path):
    """CLI overrides use model_copy; the doctor must still see the list."""
    toml = tmp_path / "platform.toml"
    toml.write_text("[platform]\n\n[analytcs]\n")
    config = PlatformConfig.from_toml(toml).model_copy(update={"apps": []})
    assert len(config.unknown_tables) == 1


def test_config_built_in_code_has_no_unknown_tables():
    assert PlatformConfig().unknown_tables == []


def test_from_toml_accepts_a_str_path(tmp_path, monkeypatch):
    """A plain string works like a Path, as the spec documents (enlace#90)."""
    (tmp_path / "papp").mkdir()
    toml = _write_toml(tmp_path / "cfg" / "platform.toml", 'apps_dirs = ["../papp"]\n')
    monkeypatch.chdir(tmp_path / "papp")

    config = PlatformConfig.from_toml(str(toml))

    assert config.apps_dirs == [(tmp_path / "papp").resolve()]
    assert PlatformConfig.from_toml(str(tmp_path / "missing.toml")).apps_dirs
