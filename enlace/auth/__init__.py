"""Authentication subsystem for enlace.

Apps never import from here. The contract exposed to mounted apps is just
``request.state.user_id`` and (optionally) ``request.state.user_email``.

Public helpers:

- ``PlatformAuthMiddleware`` — pure-ASGI auth middleware.
- ``CSRFMiddleware`` — signed double-submit CSRF.
- ``SessionStore`` — MutableMapping-backed session storage.
- ``hash_password`` / ``verify_password`` — argon2id helpers.
- ``make_auth_router`` — FastAPI router for ``/auth/*`` endpoints.
"""

from enlace.auth.cookies import sign_cookie, verify_cookie
from enlace.auth.middleware import AccessRule, CSRFMiddleware, PlatformAuthMiddleware
from enlace.auth.passwords import hash_password, verify_password
from enlace.auth.routes import make_auth_router
from enlace.auth.sessions import SessionStore

__all__ = [
    "AccessRule",
    "CSRFMiddleware",
    "PlatformAuthMiddleware",
    "SessionStore",
    "hash_password",
    "make_auth_router",
    "sign_cookie",
    "verify_cookie",
    "verify_password",
]
