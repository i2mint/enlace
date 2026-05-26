"""Integration tests for the full enlace pipeline."""

import textwrap

from starlette.testclient import TestClient

from enlace.base import PlatformConfig
from enlace.compose import build_backend
from enlace.discover import discover_apps


def test_full_pipeline(tmp_path):
    """End-to-end: create apps dir, discover, compose, serve, verify response."""
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()

    # Create a simple app
    foo_dir = apps_dir / "foo"
    foo_dir.mkdir()
    (foo_dir / "server.py").write_text(
        textwrap.dedent("""\
            from fastapi import FastAPI

            app = FastAPI()

            @app.get("/hello")
            def hello():
                return {"message": "Hello from foo"}
        """)
    )

    # Create a second app
    bar_dir = apps_dir / "bar"
    bar_dir.mkdir()
    (bar_dir / "server.py").write_text(
        textwrap.dedent("""\
            from fastapi import FastAPI

            app = FastAPI()

            @app.get("/items")
            def items():
                return [{"id": 1, "name": "Widget"}]
        """)
    )

    # Full pipeline
    config = PlatformConfig(apps_dir=apps_dir)
    config = discover_apps(config)

    assert len(config.apps) == 2
    assert {a.name for a in config.apps} == {"bar", "foo"}

    app = build_backend(config)
    client = TestClient(app)

    # Verify foo
    resp = client.get("/api/foo/hello")
    assert resp.status_code == 200
    assert resp.json()["message"] == "Hello from foo"

    # Verify bar
    resp = client.get("/api/bar/items")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["name"] == "Widget"


def test_show_config_json(tmp_path):
    """show-config --json returns parseable JSON."""
    import json

    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    foo_dir = apps_dir / "foo"
    foo_dir.mkdir()
    (foo_dir / "server.py").write_text(
        textwrap.dedent("""\
            from fastapi import FastAPI
            app = FastAPI()

            @app.get("/")
            def root():
                return {"ok": True}
        """)
    )

    config = PlatformConfig(apps_dir=apps_dir)
    config = discover_apps(config)
    data = config.model_dump(mode="json")

    assert isinstance(data, dict)
    assert len(data["apps"]) == 1
    assert data["apps"][0]["name"] == "foo"
    # Verify it round-trips through JSON
    json_str = json.dumps(data)
    assert json.loads(json_str) == data


def test_trailing_slash_redirect_for_mounted_app(tmp_path):
    """Bare /api/{name} 307-redirects to /api/{name}/ for mounted sub-apps.

    Starlette mounts only match the trailing-slash form; without the redirect
    a bare /api/foo returns 404. Regression test for the typola/external-proxy
    bug where /typola (no slash) gave a hard 404 while /typola/ worked.
    """
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    foo_dir = apps_dir / "foo"
    foo_dir.mkdir()
    (foo_dir / "server.py").write_text(
        textwrap.dedent("""\
            from fastapi import FastAPI
            app = FastAPI()

            @app.get("/hello")
            def hello():
                return {"ok": True}
        """)
    )

    config = PlatformConfig(apps_dir=apps_dir)
    config = discover_apps(config)
    app = build_backend(config)
    client = TestClient(app)

    # Without the fix, /api/foo would 404; with it, redirect to /api/foo/.
    resp = client.get("/api/foo", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/api/foo/"

    # And the actual mounted route still works on the trailing-slash form.
    resp = client.get("/api/foo/hello")
    assert resp.status_code == 200
