"""Tests for the declarative [build] section: schema, discovery, runner."""

import textwrap

from enlace.base import AppConfig, BuildConfig, PlatformConfig
from enlace.build import app_dir_of, build_cwd, has_build, run_build, validate_build
from enlace.discover import discover_apps

# --- BuildConfig schema -----------------------------------------------------


def test_build_config_splits_string_commands():
    bc = BuildConfig(install="npm install --no-audit", build="npm run build")
    assert bc.install == ["npm", "install", "--no-audit"]
    assert bc.build == ["npm", "run", "build"]


def test_build_config_accepts_list_commands():
    bc = BuildConfig(build=["vite", "build"])
    assert bc.build == ["vite", "build"]


def test_build_config_defaults():
    bc = BuildConfig()
    assert bc.build is None
    assert bc.install is None
    assert bc.env_vars == []
    assert bc.outputs == []


def test_appconfig_build_defaults_none():
    app = AppConfig(name="x", route_prefix="/api/x", app_type="asgi_app")
    assert app.build is None


# --- discovery of [build] ---------------------------------------------------


def _app_with_build_toml(apps_dir, toml_body: str):
    foo = apps_dir / "foo"
    foo.mkdir()
    (foo / "server.py").write_text(
        textwrap.dedent("""\
            from fastapi import FastAPI
            app = FastAPI()
        """)
    )
    (foo / "app.toml").write_text(textwrap.dedent(toml_body))
    return foo


def test_discovery_parses_build_section(tmp_apps_dir):
    _app_with_build_toml(
        tmp_apps_dir,
        """
        [build]
        cwd = "webapp/ui"
        install = ["npm", "install"]
        build = "npm run build"
        env_vars = ["VITE_API_BASE"]
        """,
    )
    config = discover_apps(PlatformConfig(apps_dir=tmp_apps_dir))
    app = next(a for a in config.apps if a.name == "foo")
    assert app.build is not None
    assert app.build.build == ["npm", "run", "build"]
    assert app.build.install == ["npm", "install"]
    assert app.build.env_vars == ["VITE_API_BASE"]
    # cwd is resolved relative to the app dir.
    assert app.build.cwd == (tmp_apps_dir / "foo" / "webapp/ui")
    assert app.provenance.get("build") == "override: app.toml [build]"


def test_discovery_no_build_section(tmp_apps_dir):
    foo = tmp_apps_dir / "foo"
    foo.mkdir()
    (foo / "server.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    config = discover_apps(PlatformConfig(apps_dir=tmp_apps_dir))
    app = next(a for a in config.apps if a.name == "foo")
    assert app.build is None


# --- runner helpers ---------------------------------------------------------


def test_build_cwd_defaults_to_app_dir(tmp_apps_dir):
    _app_with_build_toml(tmp_apps_dir, "[build]\nbuild = 'echo hi'\n")
    config = discover_apps(PlatformConfig(apps_dir=tmp_apps_dir))
    app = next(a for a in config.apps if a.name == "foo")
    assert build_cwd(app) == tmp_apps_dir / "foo"


def test_has_build_true_only_with_build_command():
    app = AppConfig(name="x", route_prefix="/api/x", app_type="asgi_app")
    assert has_build(app) is False
    app.build = BuildConfig(install=["npm", "i"])  # no build command
    assert has_build(app) is False
    app.build = BuildConfig(build=["vite", "build"])
    assert has_build(app) is True


def test_run_build_dry_run_records_commands(tmp_apps_dir):
    _app_with_build_toml(
        tmp_apps_dir,
        "[build]\ninstall = 'npm ci'\nbuild = 'npm run build'\n",
    )
    config = discover_apps(PlatformConfig(apps_dir=tmp_apps_dir))
    app = next(a for a in config.apps if a.name == "foo")
    result = run_build(app, dry_run=True)
    assert result.ran is False
    assert result.commands == [["npm", "ci"], ["npm", "run", "build"]]


def test_run_build_executes_real_command(tmp_apps_dir):
    # Use a command that writes a marker file, then assert it ran in cwd.
    foo = _app_with_build_toml(
        tmp_apps_dir,
        "[build]\nbuild = ['python', '-c', \"open('built.txt','w').write('ok')\"]\n",
    )
    config = discover_apps(PlatformConfig(apps_dir=tmp_apps_dir))
    app = next(a for a in config.apps if a.name == "foo")
    result = run_build(app)
    assert result.ran is True
    assert result.returncode == 0
    assert (foo / "built.txt").read_text() == "ok"


def test_run_build_noop_without_build():
    app = AppConfig(name="x", route_prefix="/api/x", app_type="asgi_app")
    result = run_build(app)
    assert result.ran is False
    assert result.commands == []


def test_run_build_injects_extra_env(tmp_apps_dir):
    foo = _app_with_build_toml(
        tmp_apps_dir,
        "[build]\nbuild = ['python', '-c', "
        "\"import os; open('env.txt','w').write(os.environ['VITE_API_BASE'])\"]\n",
    )
    config = discover_apps(PlatformConfig(apps_dir=tmp_apps_dir))
    app = next(a for a in config.apps if a.name == "foo")
    run_build(app, extra_env={"VITE_API_BASE": "/api/foo"})
    assert (foo / "env.txt").read_text() == "/api/foo"


# --- validation -------------------------------------------------------------


def test_validate_build_ok(tmp_apps_dir):
    _app_with_build_toml(tmp_apps_dir, "[build]\nbuild = 'npm run build'\n")
    config = discover_apps(PlatformConfig(apps_dir=tmp_apps_dir))
    app = next(a for a in config.apps if a.name == "foo")
    assert validate_build(app) == []


def test_validate_build_flags_missing_cwd(tmp_apps_dir):
    _app_with_build_toml(
        tmp_apps_dir,
        "[build]\ncwd = 'does_not_exist'\nbuild = 'npm run build'\n",
    )
    config = discover_apps(PlatformConfig(apps_dir=tmp_apps_dir))
    app = next(a for a in config.apps if a.name == "foo")
    problems = validate_build(app)
    assert any("cwd does not exist" in p for p in problems)


def test_validate_build_flags_empty_build(tmp_apps_dir):
    _app_with_build_toml(tmp_apps_dir, "[build]\ninstall = 'npm ci'\n")
    config = discover_apps(PlatformConfig(apps_dir=tmp_apps_dir))
    app = next(a for a in config.apps if a.name == "foo")
    problems = validate_build(app)
    assert any("no 'build' command" in p for p in problems)


def test_validate_build_none_when_no_section():
    app = AppConfig(name="x", route_prefix="/api/x", app_type="asgi_app")
    assert validate_build(app) == []


def test_app_dir_of_falls_back(tmp_path):
    app = AppConfig(name="x", route_prefix="/api/x", app_type="asgi_app")
    # No source_dir/entry/frontend → current dir fallback.
    assert app_dir_of(app).name in (".", "")  # Path(".")
