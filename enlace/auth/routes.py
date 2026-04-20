"""Auth HTTP routes: register, login, logout, shared-login, csrf.

OAuth routes live in ``enlace.auth.oauth`` and are attached separately so the
Authlib dependency stays lazy.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr

from enlace.auth.cookies import sign_cookie, verify_cookie
from enlace.auth.passwords import hash_password, verify_password
from enlace.auth.sessions import SessionStore


class _LoginBody(BaseModel):
    email: EmailStr
    password: str


class _RegisterBody(BaseModel):
    email: EmailStr
    password: str


class _SharedLoginBody(BaseModel):
    app: str
    password: str


def make_auth_router(
    *,
    session_store: SessionStore,
    user_store,  # MutableMapping[email -> {password_hash, created_at}]
    signing_key: str,
    cookie_name: str = "enlace_session",
    session_max_age: int = 86400,
    secure_cookies: bool = True,
    shared_password_for: Callable[[str], Optional[str]] = lambda _: None,
) -> APIRouter:
    """Build a FastAPI router exposing ``/auth/*`` endpoints."""
    router = APIRouter(prefix="/auth")

    def _set_session_cookie(
        response: Response, value: str, *, max_age: int, salt: str, name: str
    ):
        signed = sign_cookie(value, signing_key, salt=salt)
        attrs = [
            f"{name}={signed}",
            "Path=/",
            "HttpOnly",
            f"Max-Age={max_age}",
            "SameSite=Lax",
        ]
        if secure_cookies:
            attrs.append("Secure")
        response.headers.append("set-cookie", "; ".join(attrs))

    def _clear_cookie(response: Response, name: str):
        response.headers.append(
            "set-cookie",
            f"{name}=; Path=/; Max-Age=0; SameSite=Lax"
            + ("; Secure" if secure_cookies else ""),
        )

    @router.post("/register")
    async def register(body: _RegisterBody, response: Response) -> dict[str, Any]:
        email = body.email.lower()
        if email in user_store:
            raise HTTPException(status_code=409, detail="Email already registered")
        user_store[email] = {
            "password_hash": hash_password(body.password),
            "created_at": time.time(),
        }
        session_id = session_store.create(user_id=email, email=email)
        _set_session_cookie(
            response,
            session_id,
            max_age=session_max_age,
            salt="session",
            name=cookie_name,
        )
        return {"ok": True, "email": email}

    @router.post("/login")
    async def login(body: _LoginBody, response: Response) -> dict[str, Any]:
        email = body.email.lower()
        try:
            record = user_store[email]
        except KeyError:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not isinstance(record, dict) or "password_hash" not in record:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not verify_password(record["password_hash"], body.password):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        session_id = session_store.create(user_id=email, email=email)
        _set_session_cookie(
            response,
            session_id,
            max_age=session_max_age,
            salt="session",
            name=cookie_name,
        )
        return {"ok": True, "email": email}

    @router.post("/logout")
    async def logout(request: Request, response: Response) -> dict[str, Any]:
        token = request.cookies.get(cookie_name)
        if token:
            session_id = verify_cookie(token, signing_key, salt="session")
            if session_id:
                session_store.delete(session_id)
        _clear_cookie(response, cookie_name)
        return {"ok": True}

    @router.post("/shared-login")
    async def shared_login(
        body: _SharedLoginBody, response: Response
    ) -> dict[str, Any]:
        stored_hash = shared_password_for(body.app)
        if not stored_hash:
            raise HTTPException(status_code=404, detail=f"Unknown app '{body.app}'")
        if not verify_password(stored_hash, body.password):
            raise HTTPException(status_code=401, detail="Invalid password")
        token = sign_cookie("1", signing_key, salt=f"shared:{body.app}")
        cookie_name_shared = f"shared_auth_{body.app}"
        attrs = [
            f"{cookie_name_shared}={token}",
            "Path=/",
            "HttpOnly",
            f"Max-Age={session_max_age}",
            "SameSite=Lax",
        ]
        if secure_cookies:
            attrs.append("Secure")
        response.headers.append("set-cookie", "; ".join(attrs))
        return {"ok": True, "app": body.app}

    @router.get("/whoami")
    async def whoami(request: Request) -> dict[str, Any]:
        return {
            "user_id": getattr(request.state, "user_id", None),
            "email": getattr(request.state, "user_email", None),
        }

    @router.get("/csrf")
    async def csrf(request: Request) -> dict[str, Any]:
        """Return the unsigned CSRF token.

        Three cases, in priority order:
        1. CSRFMiddleware just minted a token for this request (no inbound
           cookie). It exposes the unsigned value via ``request.state.csrf_token``
           and sets the signed cookie in the response itself — we just echo.
        2. The request already carried a valid signed cookie — unseal it and
           return the unsigned value. No new cookie is set.
        3. No cookie and no minted token (shouldn't happen in practice, but
           defensive): fall through to a 500-like empty string. Prefer to let
           the next request set the cookie via the middleware.
        """
        minted = getattr(request.state, "csrf_token", None)
        if minted:
            return {"csrf": minted}
        existing = request.cookies.get("enlace_csrf")
        if existing:
            token = verify_cookie(existing, signing_key, salt="csrf")
            if token:
                return {"csrf": token}
        # Degenerate: no cookie, no minted token. Let the client retry.
        raise HTTPException(
            status_code=503, detail="CSRF token unavailable; retry this request"
        )

    return router
