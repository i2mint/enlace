"""Store injection middleware and per-app store router.

``StoreInjectionMiddleware`` runs after the auth middleware. It reads
``scope["state"]["user_id"]`` and ``scope["state"]["app_id"]`` (the latter set
by the per-mount wrapper in ``enlace.compose``) and attaches a
``PrefixedStore(base, f"{user_id}/{app_id}/")`` to ``scope["state"]["store"]``.

If either id is missing, ``store`` is ``None`` — apps are expected to handle
that case gracefully (they do so naturally by providing a dict fallback in
standalone mode).
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Request

from enlace.stores.prefixed import PrefixedStore
from enlace.stores.validation import sanitize_key


class StoreInjectionMiddleware:
    """Pure-ASGI middleware that injects ``request.state.store``."""

    def __init__(self, app, *, base_store: Optional[MutableMapping] = None):
        self.app = app
        self._base = base_store

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        user_id = state.get("user_id")
        app_id = state.get("app_id")

        if self._base is not None and user_id and app_id:
            try:
                prefix = f"{sanitize_key(str(user_id))}/{sanitize_key(app_id)}/"
                state["store"] = PrefixedStore(self._base, prefix)
            except ValueError:
                state["store"] = None
        else:
            state["store"] = None

        await self.app(scope, receive, send)


def make_store_router(
    *,
    base_store_getter: Callable[[], Optional[MutableMapping]],
    protected_apps: set[str],
) -> APIRouter:
    """Return a router exposing ``/api/{app_id}/store/{key}`` endpoints.

    Only apps whose name is in ``protected_apps`` (i.e. ``protected:user``
    access level) can have their store accessed this way. The router assumes
    ``PlatformAuthMiddleware`` has already set ``request.state.user_id``.
    """
    router = APIRouter()

    def _scoped_store(request: Request, app_id: str) -> PrefixedStore:
        if app_id not in protected_apps:
            raise HTTPException(
                status_code=404, detail=f"No user store for app '{app_id}'"
            )
        user_id = getattr(request.state, "user_id", None)
        if not user_id:
            raise HTTPException(status_code=401, detail="Not authenticated")
        base = base_store_getter()
        if base is None:
            raise HTTPException(status_code=503, detail="User data store disabled")
        try:
            prefix = f"{sanitize_key(str(user_id))}/{sanitize_key(app_id)}/"
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return PrefixedStore(base, prefix)

    @router.get("/api/{app_id}/store/{key:path}")
    async def get_value(app_id: str, key: str, request: Request):
        store = _scoped_store(request, app_id)
        try:
            sanitize_key(key)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        try:
            return {"value": store[key]}
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Key '{key}' not found")

    @router.put("/api/{app_id}/store/{key:path}")
    async def put_value(app_id: str, key: str, request: Request):
        store = _scoped_store(request, app_id)
        try:
            sanitize_key(key)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail="Body must be JSON") from e
        value = (
            body.get("value") if isinstance(body, dict) and "value" in body else body
        )
        store[key] = value
        return {"ok": True}

    @router.delete("/api/{app_id}/store/{key:path}")
    async def delete_value(app_id: str, key: str, request: Request):
        store = _scoped_store(request, app_id)
        try:
            sanitize_key(key)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        try:
            del store[key]
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Key '{key}' not found")
        return {"ok": True}

    return router
