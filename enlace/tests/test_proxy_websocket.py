"""The proxy refuses a websocket within the ASGI protocol (enlace#61, item 4).

Websocket proxying is not implemented. The refusal used to be an HTTP 501
``http.response.start`` sent on a ``websocket`` scope, which no ASGI server
accepts there: uvicorn logged "Exception in ASGI application" and answered
500. A websocket is now refused with a ``websocket.close`` before accept,
which servers answer as an HTTP 403 without logging an error.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from enlace.proxy import make_proxy_app


def _proxy():
    return make_proxy_app(upstream="http://127.0.0.1:9", strip_prefix="/typola")


def _sent_messages(scope: dict) -> list[dict]:
    """Run the proxy on a websocket *scope*; return every message it sends."""
    sent: list[dict] = []
    incoming = [{"type": "websocket.connect"}]

    async def receive():
        if incoming:
            return incoming.pop(0)
        await asyncio.sleep(3600)  # nothing more arrives from this client

    async def send(message):
        sent.append(message)

    async def run():
        await asyncio.wait_for(_proxy()(scope, receive, send), timeout=5)

    asyncio.run(run())
    return sent


def _ws_scope(**extra) -> dict:
    return {"type": "websocket", "path": "/typola/ws", "headers": [], **extra}


def test_mounted_proxy_refuses_a_websocket_with_a_close():
    client = TestClient(Starlette(routes=[Mount("/typola", app=_proxy())]))
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/typola/ws"):
            pass
    assert excinfo.value.code == 1011


@pytest.mark.parametrize(
    "extensions", [None, {"websocket.http.response": {}}], ids=["plain", "denial-ext"]
)
def test_websocket_refusal_sends_only_a_websocket_close(extensions):
    scope = _ws_scope() if extensions is None else _ws_scope(extensions=extensions)
    assert _sent_messages(scope) == [{"type": "websocket.close", "code": 1011}]
