"""StoreInjectionMiddleware and store router."""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from enlace.stores import (
    PrefixedStore,
    StoreInjectionMiddleware,
    make_store_router,
)


class _Probe:
    """Terminal ASGI app that records scope state for assertions."""

    def __init__(self):
        self.state = None

    async def __call__(self, scope, receive, send):
        self.state = dict(scope.get("state", {}))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def _scope(state):
    return {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": [],
        "state": state,
    }


async def _noop_receive():
    return {"type": "http.request"}


async def _noop_send(_):
    return


def test_injection_with_user_and_app_id():
    base: dict = {}
    probe = _Probe()
    mw = StoreInjectionMiddleware(probe, base_store=base)
    asyncio.run(
        mw(_scope({"user_id": "alice", "app_id": "chord"}), _noop_receive, _noop_send)
    )
    assert isinstance(probe.state["store"], PrefixedStore)
    probe.state["store"]["k"] = 1
    assert base["alice/chord/k"] == 1


def test_injection_none_when_no_user():
    probe = _Probe()
    mw = StoreInjectionMiddleware(probe, base_store={})
    asyncio.run(
        mw(_scope({"user_id": None, "app_id": "chord"}), _noop_receive, _noop_send)
    )
    assert probe.state["store"] is None


def test_injection_none_when_no_base():
    probe = _Probe()
    mw = StoreInjectionMiddleware(probe, base_store=None)
    asyncio.run(
        mw(_scope({"user_id": "alice", "app_id": "chord"}), _noop_receive, _noop_send)
    )
    assert probe.state["store"] is None


def test_injection_none_when_user_id_unsafe():
    probe = _Probe()
    mw = StoreInjectionMiddleware(probe, base_store={})
    asyncio.run(
        mw(
            _scope({"user_id": "../escape", "app_id": "chord"}),
            _noop_receive,
            _noop_send,
        )
    )
    assert probe.state["store"] is None


def test_store_router_roundtrip():
    base: dict = {}
    app = FastAPI()

    @app.middleware("http")
    async def _fake_auth(request, call_next):
        request.state.user_id = "alice"
        return await call_next(request)

    router = make_store_router(
        base_store_getter=lambda: base,
        protected_apps={"chord"},
    )
    app.include_router(router)
    client = TestClient(app)

    # Put
    r = client.put("/api/chord/store/settings", json={"value": {"color": "blue"}})
    assert r.status_code == 200
    # Get
    r = client.get("/api/chord/store/settings")
    assert r.status_code == 200
    assert r.json()["value"] == {"color": "blue"}
    # Missing
    r = client.get("/api/chord/store/nope")
    assert r.status_code == 404
    # Delete
    r = client.delete("/api/chord/store/settings")
    assert r.status_code == 200
    # Unknown app
    r = client.get("/api/unknown/store/x")
    assert r.status_code == 404
    # Unsafe key
    r = client.get("/api/chord/store/..")
    # FastAPI path matching may decode differently — accept either 400 or 404.
    assert r.status_code in (400, 404)
