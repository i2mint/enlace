"""End-to-end test for the auth + stores pipeline.

Builds a platform with two apps — one public, one ``protected:user`` — and
exercises register/login/protected-access/store-roundtrip/logout through a
live ``TestClient``.
"""

from __future__ import annotations

import textwrap

import pytest
from starlette.testclient import TestClient

from enlace.auth import hash_password
from enlace.base import PlatformConfig
from enlace.compose import build_backend
from enlace.discover import discover_apps


def _make_apps(apps_dir):
    """Write two apps: one public, one protected, plus one shared-password."""
    public = apps_dir / "public_app"
    public.mkdir()
    (public / "server.py").write_text(
        textwrap.dedent(
            """
            from fastapi import FastAPI, Request
            app = FastAPI()
            @app.get("/ping")
            def ping():
                return {"ok": True}
            @app.get("/who")
            def who(request: Request):
                return {"user_id": getattr(request.state, "user_id", None)}
            """
        ).strip()
    )

    private = apps_dir / "private_app"
    private.mkdir()
    (private / "server.py").write_text(
        textwrap.dedent(
            """
            from fastapi import FastAPI, Request
            app = FastAPI()
            @app.get("/me")
            def me(request: Request):
                store = getattr(request.state, "store", None)
                return {
                    "user_id": request.state.user_id,
                    "email": getattr(request.state, "user_email", None),
                    "store_kind": type(store).__name__ if store is not None else None,
                }
            """
        ).strip()
    )
    (private / "app.toml").write_text('access = "protected:user"\n')

    shared = apps_dir / "shared_app"
    shared.mkdir()
    (shared / "server.py").write_text(
        textwrap.dedent(
            """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/peek")
            def peek():
                return {"ok": True}
            """
        ).strip()
    )
    (shared / "app.toml").write_text(
        'access = "protected:shared"\nshared_password_env = "SHARED_APP_PW"\n'
    )


@pytest.fixture
def e2e_client(tmp_path, monkeypatch):
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    _make_apps(apps_dir)

    # Signing key + shared password hash live in env vars.
    monkeypatch.setenv("ENLACE_SIGNING_KEY", "e2e-key-32bytes-minimumlength!!!")
    monkeypatch.setenv("SHARED_APP_PW", hash_password("open-sesame"))

    platform_store = tmp_path / "platform_store"
    user_data = tmp_path / "user_data"

    config = PlatformConfig(
        apps_dir=apps_dir,
        auth={
            "enabled": True,
            "secure_cookies": False,
            "stores": {"backend": "file", "path": str(platform_store)},
        },
        stores={"user_data": {"backend": "file", "path": str(user_data)}},
    )
    config = discover_apps(config)
    app = build_backend(config)
    return TestClient(app)


def _csrf_pair(client: TestClient) -> tuple[str, str]:
    """Do a safe GET so the server issues a CSRF cookie, then extract it."""
    r = client.get("/api/public_app/ping")
    assert r.status_code == 200
    signed = client.cookies.get("enlace_csrf")
    assert signed is not None
    from enlace.auth.cookies import verify_cookie

    raw = verify_cookie(signed, "e2e-key-32bytes-minimumlength!!!", salt="csrf")
    assert raw is not None
    return raw, signed


def test_public_app_accessible_without_login(e2e_client):
    r = e2e_client.get("/api/public_app/ping")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_protected_app_denied_without_session(e2e_client):
    r = e2e_client.get("/api/private_app/me")
    assert r.status_code == 401


def test_register_login_access_store_logout(e2e_client):
    raw, _ = _csrf_pair(e2e_client)
    headers = {"X-CSRF-Token": raw}

    r = e2e_client.post(
        "/auth/register",
        json={"email": "alice@example.com", "password": "secretpw123"},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    # Now a protected endpoint works.
    r = e2e_client.get("/api/private_app/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "alice@example.com"

    # Store round-trip via the platform /api/{app}/store endpoint.
    r = e2e_client.put(
        "/api/private_app/store/settings",
        json={"value": {"color": "blue"}},
        headers=headers,
    )
    assert r.status_code == 200
    r = e2e_client.get("/api/private_app/store/settings")
    assert r.status_code == 200
    assert r.json()["value"] == {"color": "blue"}

    # Logout clears the session.
    r = e2e_client.post("/auth/logout", headers=headers)
    assert r.status_code == 200
    # Protected access now denied again.
    e2e_client.cookies.clear()
    r = e2e_client.get("/api/private_app/me")
    assert r.status_code == 401


def test_login_wrong_password_rejected(e2e_client):
    raw, _ = _csrf_pair(e2e_client)
    headers = {"X-CSRF-Token": raw}
    r = e2e_client.post(
        "/auth/register",
        json={"email": "bob@example.com", "password": "correct"},
        headers=headers,
    )
    assert r.status_code == 200
    e2e_client.cookies.clear()
    raw, _ = _csrf_pair(e2e_client)
    headers = {"X-CSRF-Token": raw}
    r = e2e_client.post(
        "/auth/login",
        json={"email": "bob@example.com", "password": "wrong"},
        headers=headers,
    )
    assert r.status_code == 401


def test_shared_password_flow(e2e_client):
    raw, _ = _csrf_pair(e2e_client)
    headers = {"X-CSRF-Token": raw}
    r = e2e_client.get("/api/shared_app/peek")
    assert r.status_code == 401

    r = e2e_client.post(
        "/auth/shared-login",
        json={"app": "shared_app", "password": "open-sesame"},
        headers=headers,
    )
    assert r.status_code == 200

    r = e2e_client.get("/api/shared_app/peek")
    assert r.status_code == 200


def test_identity_header_stripped(e2e_client):
    """A spoofed X-User-ID header must not leak into request.state.user_id."""
    r = e2e_client.get(
        "/api/public_app/who",
        headers={"X-User-ID": "attacker"},
    )
    assert r.status_code == 200
    # Public app sets no session, so user_id is None even with header present.
    assert r.json()["user_id"] is None
