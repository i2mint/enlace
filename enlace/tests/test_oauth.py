"""OAuth routes with a mocked Authlib registry."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse

from enlace.auth import SessionStore
from enlace.auth.cookies import verify_cookie
from enlace.base import OAuthProviderConfig

SIGNING_KEY = "oauth-signing-key-32bytes-minlen"


def _make_app(providers, user_store, session_store):
    """Build a FastAPI app with the OAuth router and env vars set."""
    os.environ["G_ID"] = "fake-client-id"
    os.environ["G_SECRET"] = "fake-client-secret"
    from enlace.auth.oauth import make_oauth_router

    # Patch Authlib's OAuth registry so we don't hit the network.
    fake_client = MagicMock()
    fake_client.authorize_redirect = AsyncMock(
        return_value=RedirectResponse("http://example.com/fake-authorize")
    )
    fake_client.authorize_access_token = AsyncMock(
        return_value={"userinfo": {"email": "alice@example.com"}}
    )
    fake_registry = MagicMock()
    fake_registry.google = fake_client

    with patch("enlace.auth.oauth._build_oauth_registry", return_value=fake_registry):
        router = make_oauth_router(
            providers=providers,
            session_store=session_store,
            user_store=user_store,
            signing_key=SIGNING_KEY,
            secure_cookies=False,
        )

    app = FastAPI()
    app.include_router(router)
    return app, fake_client


def test_oauth_login_redirects():
    providers = {
        "google": OAuthProviderConfig(
            client_id_env="G_ID", client_secret_env="G_SECRET"
        )
    }
    users: dict = {}
    sessions = SessionStore({})
    app, _ = _make_app(providers, users, sessions)
    client = TestClient(app)
    r = client.get("/auth/login/google", follow_redirects=False)
    assert r.status_code in (302, 307)


def test_oauth_callback_creates_session_and_cookie():
    providers = {
        "google": OAuthProviderConfig(
            client_id_env="G_ID", client_secret_env="G_SECRET"
        )
    }
    users: dict = {}
    sessions = SessionStore({})
    app, _ = _make_app(providers, users, sessions)
    client = TestClient(app)

    r = client.get("/auth/callback/google")
    assert r.status_code == 200
    assert "alice@example.com" in users
    # A session cookie should be set, pointing at a valid session.
    set_cookie = r.headers.get("set-cookie", "")
    assert "enlace_session=" in set_cookie
    token = set_cookie.split("enlace_session=")[1].split(";")[0]
    sid = verify_cookie(token, SIGNING_KEY, salt="session")
    assert sid is not None
    assert sessions.get(sid) is not None


def test_no_providers_returns_none():
    from enlace.auth.oauth import make_oauth_router

    assert (
        make_oauth_router(
            providers={},
            session_store=SessionStore({}),
            user_store={},
            signing_key=SIGNING_KEY,
        )
        is None
    )
