"""Tests for enlace.discover — convention-based app discovery."""

from pathlib import Path

import pytest

from enlace.base import ConventionsConfig, PlatformConfig
from enlace.discover import ConventionDiscoverer, discover_apps
from enlace.tests.conftest import (
    BROKEN_MODULE,
    FUNCTIONS_MODULE,
    _make_app_code,
)


def _make_discoverer():
    return ConventionDiscoverer(ConventionsConfig())


def test_discover_single_app(single_app_dir):
    """A directory with server.py containing a FastAPI app is discovered."""
    discoverer = _make_discoverer()
    apps = discoverer.discover(single_app_dir)

    assert len(apps) == 1
    app = apps[0]
    assert app.name == "foo"
    assert app.route_prefix == "/api/foo"
    assert app.app_type == "asgi_app"
    assert app.display_name == "Foo"
    assert "route_prefix" in app.provenance


def test_discover_multiple_apps(multi_app_dir):
    """Multiple app directories are all discovered in sorted order."""
    discoverer = _make_discoverer()
    apps = discoverer.discover(multi_app_dir)

    assert len(apps) == 3
    assert [a.name for a in apps] == ["alpha", "beta", "gamma"]


def test_discover_skips_hidden(tmp_apps_dir):
    """Directories starting with '.' are skipped."""
    hidden = tmp_apps_dir / ".git"
    hidden.mkdir()
    (hidden / "server.py").write_text(_make_app_code("git"))

    discoverer = _make_discoverer()
    apps = discoverer.discover(tmp_apps_dir)
    assert len(apps) == 0


def test_discover_skips_private(tmp_apps_dir):
    """Directories starting with '_' are skipped."""
    private = tmp_apps_dir / "_internal"
    private.mkdir()
    (private / "server.py").write_text(_make_app_code("internal"))

    discoverer = _make_discoverer()
    apps = discoverer.discover(tmp_apps_dir)
    assert len(apps) == 0


def test_discover_entry_point_priority(tmp_apps_dir):
    """server.py takes priority over app.py when both exist."""
    app_dir = tmp_apps_dir / "myapp"
    app_dir.mkdir()
    # server.py has a specific message
    (app_dir / "server.py").write_text(_make_app_code("from_server"))
    (app_dir / "app.py").write_text(_make_app_code("from_app"))

    discoverer = _make_discoverer()
    apps = discoverer.discover(tmp_apps_dir)

    assert len(apps) == 1
    assert apps[0].entry_module_path.name == "server.py"


def test_discover_no_entry_point_skipped(tmp_apps_dir):
    """A directory with no recognized entry file is skipped."""
    empty_dir = tmp_apps_dir / "empty"
    empty_dir.mkdir()
    (empty_dir / "README.md").write_text("Not an app")

    discoverer = _make_discoverer()
    apps = discoverer.discover(tmp_apps_dir)
    assert len(apps) == 0


def test_discover_app_toml_override(single_app_dir):
    """Per-app TOML overrides are applied and provenance is tracked."""
    override_toml = single_app_dir / "foo" / "app.toml"
    override_toml.write_text(
        'route = "/api/custom"\ndisplay_name = "My Custom Foo"\naccess = "public"\n'
    )

    discoverer = _make_discoverer()
    apps = discoverer.discover(single_app_dir)

    assert len(apps) == 1
    app = apps[0]
    assert app.route_prefix == "/api/custom"
    assert app.display_name == "My Custom Foo"
    assert app.access == "public"
    assert app.provenance["route_prefix"] == "override: app.toml"


def test_discover_conflict_detection(tmp_apps_dir):
    """Two apps resolving to the same route trigger a conflict."""
    for name in ["app_a", "app_b"]:
        d = tmp_apps_dir / name
        d.mkdir()
        (d / "server.py").write_text(_make_app_code(name))
        # Both override to the same route
        (d / "app.toml").write_text('route = "/api/shared"\n')

    discoverer = _make_discoverer()
    apps = discoverer.discover(tmp_apps_dir)
    config = PlatformConfig(apps=apps)
    errors = config.check_conflicts()

    assert len(errors) == 1
    assert "shared" in errors[0]
    assert "app_a" in errors[0]
    assert "app_b" in errors[0]


def test_discover_import_error_propagates(tmp_apps_dir):
    """A module with a genuine import error is NOT silently swallowed."""
    broken_dir = tmp_apps_dir / "broken"
    broken_dir.mkdir()
    (broken_dir / "server.py").write_text(BROKEN_MODULE)

    discoverer = _make_discoverer()
    with pytest.raises(ModuleNotFoundError, match="nonexistent_package_xyz"):
        discoverer.discover(tmp_apps_dir)


def test_discover_functions_module(tmp_apps_dir):
    """A module with typed functions but no app attr is detected as 'functions'."""
    func_dir = tmp_apps_dir / "calc"
    func_dir.mkdir()
    (func_dir / "server.py").write_text(FUNCTIONS_MODULE)

    discoverer = _make_discoverer()
    apps = discoverer.discover(tmp_apps_dir)

    assert len(apps) == 1
    assert apps[0].app_type == "functions"


def test_discover_frontend_only(tmp_apps_dir):
    """A directory with only frontend assets is detected as frontend_only."""
    blog_dir = tmp_apps_dir / "blog"
    blog_dir.mkdir()
    frontend = blog_dir / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html><body>Blog</body></html>")

    discoverer = _make_discoverer()
    apps = discoverer.discover(tmp_apps_dir)

    assert len(apps) == 1
    assert apps[0].app_type == "frontend_only"
    assert apps[0].frontend_dir is not None


def test_discover_nonexistent_dir():
    """Discovering from a non-existent directory returns empty list."""
    discoverer = _make_discoverer()
    apps = discoverer.discover(Path("/nonexistent/path"))
    assert apps == []


# --- Multi-source discovery tests ---


def test_discover_multi_source(multi_source_dirs):
    """Apps from multiple container directories are all discovered."""
    source_a, source_b = multi_source_dirs
    config = PlatformConfig(apps_dirs=[source_a, source_b])
    config = discover_apps(config)

    assert len(config.apps) == 4
    assert [a.name for a in config.apps] == ["alpha", "beta", "delta", "gamma"]


def test_discover_individual_app_dir(standalone_app_dir):
    """A standalone app directory (the dir IS the app) is discovered."""
    config = PlatformConfig(app_dirs=[standalone_app_dir])
    config = discover_apps(config)

    assert len(config.apps) == 1
    assert config.apps[0].name == "my_standalone_app"
    assert config.apps[0].route_prefix == "/api/my_standalone_app"


def test_discover_mixed_sources(multi_source_dirs, standalone_app_dir):
    """Both container dirs and individual app dirs work together."""
    source_a, _ = multi_source_dirs
    config = PlatformConfig(
        apps_dirs=[source_a],
        app_dirs=[standalone_app_dir],
    )
    config = discover_apps(config)

    assert len(config.apps) == 3
    names = [a.name for a in config.apps]
    assert "alpha" in names
    assert "beta" in names
    assert "my_standalone_app" in names


def test_discover_duplicate_name_conflict(tmp_path):
    """Same app name in two source dirs raises a conflict error."""
    source_a = tmp_path / "source_a"
    source_a.mkdir()
    (source_a / "foo").mkdir()
    (source_a / "foo" / "server.py").write_text(_make_app_code("foo_a"))

    source_b = tmp_path / "source_b"
    source_b.mkdir()
    (source_b / "foo").mkdir()
    (source_b / "foo" / "server.py").write_text(_make_app_code("foo_b"))

    config = PlatformConfig(apps_dirs=[source_a, source_b])
    with pytest.raises(RuntimeError, match="Name conflict.*foo"):
        discover_apps(config)


def test_discover_source_dir_populated(single_app_dir):
    """source_dir is set on each discovered AppConfig."""
    config = PlatformConfig(apps_dir=single_app_dir)
    config = discover_apps(config)

    assert len(config.apps) == 1
    assert config.apps[0].source_dir == single_app_dir
    assert "source_dir" in config.apps[0].provenance


# --- Non-asgi mode discovery tests ---


def test_discover_process_mode_from_toml(tmp_apps_dir):
    """A process-mode app is discovered from app.toml without any Python files."""
    node_dir = tmp_apps_dir / "blog"
    node_dir.mkdir()
    (node_dir / "app.toml").write_text(
        'mode = "process"\ncommand = ["node", "server.js"]\nport = 3001\n'
    )
    (node_dir / "server.js").write_text("// Node.js app")

    discoverer = _make_discoverer()
    apps = discoverer.discover(tmp_apps_dir)

    assert len(apps) == 1
    app = apps[0]
    assert app.name == "blog"
    assert app.mode == "process"
    assert app.command == ["node", "server.js"]
    assert app.port == 3001
    assert app.entry_module_path is None  # No Python import
    assert app.provenance["mode"] == "override: app.toml"


def test_discover_process_mode_command_string(tmp_apps_dir):
    """A string command in TOML is split via shlex."""
    node_dir = tmp_apps_dir / "api"
    node_dir.mkdir()
    (node_dir / "app.toml").write_text(
        'mode = "process"\ncommand = "uvicorn myapp:app --host 0.0.0.0"\nport = 8001\n'
    )

    discoverer = _make_discoverer()
    apps = discoverer.discover(tmp_apps_dir)

    assert len(apps) == 1
    assert apps[0].command == ["uvicorn", "myapp:app", "--host", "0.0.0.0"]


def test_discover_external_mode_from_toml(tmp_apps_dir):
    """An external-mode app is discovered from app.toml."""
    ext_dir = tmp_apps_dir / "dashboard"
    ext_dir.mkdir()
    (ext_dir / "app.toml").write_text(
        'mode = "external"\nupstream_url = "http://192.168.1.50:3000"\n'
    )

    discoverer = _make_discoverer()
    apps = discoverer.discover(tmp_apps_dir)

    assert len(apps) == 1
    app = apps[0]
    assert app.name == "dashboard"
    assert app.mode == "external"
    assert app.upstream_url == "http://192.168.1.50:3000"
    assert app.entry_module_path is None


def test_discover_static_mode_from_toml(tmp_apps_dir):
    """A static-mode app is discovered from app.toml."""
    docs_dir = tmp_apps_dir / "docs"
    docs_dir.mkdir()
    (docs_dir / "app.toml").write_text('mode = "static"\npublic_dir = "dist"\n')
    (docs_dir / "dist").mkdir()
    (docs_dir / "dist" / "index.html").write_text("<html>Docs</html>")

    discoverer = _make_discoverer()
    apps = discoverer.discover(tmp_apps_dir)

    assert len(apps) == 1
    app = apps[0]
    assert app.name == "docs"
    assert app.mode == "static"
    assert app.app_type == "frontend_only"
    assert app.public_dir == docs_dir / "dist"


def test_discover_process_mode_missing_command_raises(tmp_apps_dir):
    """Process mode without command in app.toml raises a validation error."""
    bad_dir = tmp_apps_dir / "bad"
    bad_dir.mkdir()
    (bad_dir / "app.toml").write_text('mode = "process"\nport = 3000\n')

    discoverer = _make_discoverer()
    with pytest.raises(Exception, match="requires 'command'"):
        discoverer.discover(tmp_apps_dir)


def test_discover_asgi_mode_explicit_still_imports(single_app_dir):
    """Explicitly setting mode='asgi' in app.toml still imports the Python module."""
    (single_app_dir / "foo" / "app.toml").write_text('mode = "asgi"\n')

    discoverer = _make_discoverer()
    apps = discoverer.discover(single_app_dir)

    assert len(apps) == 1
    assert apps[0].mode == "asgi"
    assert apps[0].app_type == "asgi_app"
    assert apps[0].entry_module_path is not None


def test_discover_process_mode_skips_python_import(tmp_apps_dir):
    """Process mode does not attempt to import Python — even if server.py exists."""
    app_dir = tmp_apps_dir / "heavy"
    app_dir.mkdir()
    # server.py has a broken import — but it should never be imported
    (app_dir / "server.py").write_text(BROKEN_MODULE)
    (app_dir / "app.toml").write_text(
        'mode = "process"\ncommand = ["python", "-m", "uvicorn", "heavy:app"]\n'
        "port = 8002\n"
    )

    discoverer = _make_discoverer()
    apps = discoverer.discover(tmp_apps_dir)

    assert len(apps) == 1
    assert apps[0].mode == "process"
    # The broken import was NOT triggered


def test_discover_mixed_asgi_and_process(tmp_apps_dir):
    """Container directory with both asgi and process apps discovers both."""
    # asgi app
    py_dir = tmp_apps_dir / "alpha"
    py_dir.mkdir()
    (py_dir / "server.py").write_text(_make_app_code("alpha"))

    # process app
    node_dir = tmp_apps_dir / "beta"
    node_dir.mkdir()
    (node_dir / "app.toml").write_text(
        'mode = "process"\ncommand = ["node", "index.js"]\nport = 9100\n'
    )

    discoverer = _make_discoverer()
    apps = discoverer.discover(tmp_apps_dir)

    assert len(apps) == 2
    modes = {a.name: a.mode for a in apps}
    assert modes == {"alpha": "asgi", "beta": "process"}
