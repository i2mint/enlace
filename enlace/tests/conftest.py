"""Shared test fixtures for enlace unit tests."""

import textwrap
from pathlib import Path

import pytest

from enlace.base import ConventionsConfig, PlatformConfig

def _make_app_code(name: str) -> str:
    return textwrap.dedent(f'''\
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/hello")
        def hello():
            return {{"message": "Hello from {name}"}}

        @app.get("/health")
        def health():
            return {{"status": "ok"}}
    ''')

FUNCTIONS_MODULE = textwrap.dedent("""\
    def greet(name: str) -> dict:
        return {"greeting": f"Hello, {name}!"}

    def add(a: int, b: int) -> dict:
        return {"result": a + b}
""")

BROKEN_MODULE = textwrap.dedent("""\
    import nonexistent_package_xyz
""")


@pytest.fixture
def tmp_apps_dir(tmp_path):
    """Create a temporary apps directory with a single FastAPI app."""
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    return apps_dir


@pytest.fixture
def single_app_dir(tmp_apps_dir):
    """Create a single app 'foo' with a FastAPI server."""
    foo_dir = tmp_apps_dir / "foo"
    foo_dir.mkdir()
    (foo_dir / "server.py").write_text(_make_app_code("foo"))
    return tmp_apps_dir


@pytest.fixture
def multi_app_dir(tmp_apps_dir):
    """Create multiple apps for testing."""
    for name in ["alpha", "beta", "gamma"]:
        app_dir = tmp_apps_dir / name
        app_dir.mkdir()
        (app_dir / "server.py").write_text(_make_app_code(name))
    return tmp_apps_dir


@pytest.fixture
def multi_source_dirs(tmp_path):
    """Create two separate source directories, each containing apps."""
    source_a = tmp_path / "source_a"
    source_a.mkdir()
    for name in ["alpha", "beta"]:
        d = source_a / name
        d.mkdir()
        (d / "server.py").write_text(_make_app_code(name))

    source_b = tmp_path / "source_b"
    source_b.mkdir()
    for name in ["gamma", "delta"]:
        d = source_b / name
        d.mkdir()
        (d / "server.py").write_text(_make_app_code(name))

    return source_a, source_b


@pytest.fixture
def standalone_app_dir(tmp_path):
    """Create a standalone app directory (the dir IS the app)."""
    app_dir = tmp_path / "my_standalone_app"
    app_dir.mkdir()
    (app_dir / "server.py").write_text(_make_app_code("my_standalone_app"))
    return app_dir


@pytest.fixture
def sample_config():
    """Return a PlatformConfig with defaults."""
    return PlatformConfig()
