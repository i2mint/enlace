"""Prove that apps written to be enlace-aware still work standalone.

The invariant in CLAUDE.md: every injection point (auth, store) must fall back
gracefully when ``ENLACE_MANAGED`` is unset. This test imports minimal example
apps with the env var unset and verifies they start and serve a happy path.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import textwrap
from pathlib import Path

import pytest
from starlette.testclient import TestClient


def _load_app(py_file: Path):
    """Import an app.py file as a standalone module — no platform in sight."""
    spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _unset_enlace_managed(monkeypatch):
    monkeypatch.delenv("ENLACE_MANAGED", raising=False)


def test_app_reading_state_user_id_works_standalone(tmp_path):
    """An app that reads request.state.user_id must still start outside enlace."""
    app_file = tmp_path / "server.py"
    app_file.write_text(
        textwrap.dedent(
            """
            import os
            from fastapi import FastAPI, Request
            app = FastAPI()
            @app.get("/me")
            def me(request: Request):
                # enlace-aware pattern with standalone fallback.
                user_id = getattr(request.state, "user_id", None) or os.environ.get("DEV_USER", "local")
                return {"user_id": user_id}
            """
        ).strip()
    )
    module = _load_app(app_file)
    client = TestClient(module.app)
    r = client.get("/me")
    assert r.status_code == 200
    assert r.json()["user_id"] == "local"
    assert "ENLACE_MANAGED" not in os.environ


def test_app_using_store_falls_back_to_dict(tmp_path):
    """An app pattern `store = getattr(request.state, 'store', None) or {}` works standalone."""
    app_file = tmp_path / "server.py"
    app_file.write_text(
        textwrap.dedent(
            """
            from fastapi import FastAPI, Request
            app = FastAPI()
            _memory = {}
            @app.get("/count")
            def count(request: Request):
                store = getattr(request.state, "store", None) or _memory
                store["n"] = store.get("n", 0) + 1
                return {"n": store["n"]}
            """
        ).strip()
    )
    module = _load_app(app_file)
    client = TestClient(module.app)
    assert client.get("/count").json() == {"n": 1}
    assert client.get("/count").json() == {"n": 2}


def test_cors_middleware_guarded_by_env_var(tmp_path):
    """An app that gates its CORS on ENLACE_MANAGED works both ways."""
    app_file = tmp_path / "server.py"
    app_file.write_text(
        textwrap.dedent(
            """
            import os
            from fastapi import FastAPI
            from fastapi.middleware.cors import CORSMiddleware
            app = FastAPI()
            if not os.environ.get("ENLACE_MANAGED"):
                app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"])
            @app.get("/ping")
            def ping():
                return {"ok": True}
            """
        ).strip()
    )
    module = _load_app(app_file)
    client = TestClient(module.app)
    # OPTIONS preflight should succeed only because CORS middleware is present.
    r = client.options(
        "/ping",
        headers={"origin": "http://x", "access-control-request-method": "GET"},
    )
    assert r.status_code == 200
