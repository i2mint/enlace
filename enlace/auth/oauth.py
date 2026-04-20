"""OAuth2/OIDC login via Authlib.

Lazy import — ``authlib`` lives behind the ``enlace[oauth]`` extra. Providers
are configured in ``platform.toml`` under ``[auth.oauth.{name}]`` with
``client_id_env`` / ``client_secret_env`` pointing at env vars (secrets never
in TOML). On callback we create a local session — the upstream tokens are
discarded because we use OAuth for identity only, not API access.

Built-in provider presets for Google and GitHub auto-fill the well-known
endpoints; other providers need explicit URLs in the config.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, Response

from enlace.auth.cookies import sign_cookie
from enlace.auth.sessions import SessionStore
from enlace.base import OAuthProviderConfig

_PROVIDER_PRESETS = {
    "google": {
        "server_metadata_url": "https://accounts.google.com/.well-known/openid-configuration",
        "scopes": ["openid", "profile", "email"],
    },
    "github": {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "scopes": ["read:user", "user:email"],
    },
}


def _import_authlib():
    try:
        from authlib.integrations.starlette_client import OAuth  # type: ignore
    except ImportError as e:
        raise ImportError(
            "authlib is required for OAuth. Install via `pip install enlace[oauth]`."
        ) from e
    return OAuth


def _build_oauth_registry(providers: dict[str, OAuthProviderConfig]):
    OAuth = _import_authlib()
    oauth = OAuth()
    for name, cfg in providers.items():
        preset = _PROVIDER_PRESETS.get(name, {})
        client_id = os.environ.get(cfg.client_id_env)
        client_secret = os.environ.get(cfg.client_secret_env)
        if not client_id or not client_secret:
            # Skip providers whose env vars aren't set; `enlace check` surfaces this.
            continue
        kwargs: dict[str, Any] = {
            "name": name,
            "client_id": client_id,
            "client_secret": client_secret,
            "client_kwargs": {
                "scope": " ".join(cfg.scopes or preset.get("scopes", [])),
            },
        }
        smu = cfg.server_metadata_url or preset.get("server_metadata_url")
        if smu:
            kwargs["server_metadata_url"] = smu
        else:
            if cfg.authorize_url or preset.get("authorize_url"):
                kwargs["authorize_url"] = cfg.authorize_url or preset.get(
                    "authorize_url"
                )
            if cfg.token_url or preset.get("token_url"):
                kwargs["access_token_url"] = cfg.token_url or preset.get("token_url")
            if cfg.userinfo_url or preset.get("userinfo_url"):
                kwargs["userinfo_endpoint"] = cfg.userinfo_url or preset.get(
                    "userinfo_url"
                )
        oauth.register(**kwargs)
    return oauth


def make_oauth_router(
    *,
    providers: dict[str, OAuthProviderConfig],
    session_store: SessionStore,
    user_store,  # MutableMapping[email -> {...}]
    signing_key: str,
    cookie_name: str = "enlace_session",
    session_max_age: int = 86400,
    secure_cookies: bool = True,
) -> Optional[APIRouter]:
    """Build an OAuth router or return None if no providers are configured."""
    if not providers:
        return None

    oauth = _build_oauth_registry(providers)
    router = APIRouter(prefix="/auth")

    def _set_session_cookie(response: Response, session_id: str):
        signed = sign_cookie(session_id, signing_key, salt="session")
        attrs = [
            f"{cookie_name}={signed}",
            "Path=/",
            "HttpOnly",
            f"Max-Age={session_max_age}",
            "SameSite=Lax",
        ]
        if secure_cookies:
            attrs.append("Secure")
        response.headers.append("set-cookie", "; ".join(attrs))

    @router.get("/login/{provider}")
    async def login(provider: str, request: Request):
        client = getattr(oauth, provider, None)
        if client is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown provider '{provider}'"
            )
        redirect_uri = str(request.url_for("oauth_callback", provider=provider))
        return await client.authorize_redirect(request, redirect_uri)

    @router.get("/callback/{provider}", name="oauth_callback")
    async def callback(provider: str, request: Request):
        client = getattr(oauth, provider, None)
        if client is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown provider '{provider}'"
            )
        try:
            token = await client.authorize_access_token(request)
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"OAuth failed: {e}") from e

        email = None
        userinfo = token.get("userinfo") if isinstance(token, dict) else None
        if userinfo and isinstance(userinfo, dict):
            email = userinfo.get("email")

        if not email and hasattr(client, "userinfo"):
            try:
                info = await client.userinfo(token=token)
                if isinstance(info, dict):
                    email = info.get("email")
            except Exception:
                pass

        if not email:
            raise HTTPException(status_code=401, detail="No email from OAuth provider")

        email = email.lower()
        if email not in user_store:
            user_store[email] = {
                "password_hash": None,
                "created_at": time.time(),
                "oauth_provider": provider,
            }
        session_id = session_store.create(user_id=email, email=email)
        resp = Response(
            content=f'{{"ok":true,"email":"{email}"}}',
            media_type="application/json",
        )
        _set_session_cookie(resp, session_id)
        return resp

    return router
