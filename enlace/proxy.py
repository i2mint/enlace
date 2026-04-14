"""Lightweight ASGI reverse proxy for process and external backends.

Forwards HTTP requests to an upstream server, stripping the mount prefix
from the path.  Uses ``httpx`` when available, falling back to a stdlib
implementation for simple cases.

This module is lazy-loaded: it only imports ``httpx`` when a proxy ASGI
app is actually instantiated, so the dependency remains optional.
"""

from typing import Optional


def make_proxy_app(*, upstream: str, strip_prefix: str = ""):
    """Create an ASGI app that proxies requests to *upstream*.

    Args:
        upstream: Base URL of the upstream server (e.g. ``http://127.0.0.1:9100``).
        strip_prefix: Route prefix to strip before forwarding
            (e.g. ``/api/blog`` → upstream receives ``/``).

    Returns:
        An ASGI callable.
    """
    return _HttpxProxy(upstream=upstream, strip_prefix=strip_prefix)


class _HttpxProxy:
    """Pure-ASGI reverse proxy backed by ``httpx.AsyncClient``."""

    def __init__(self, *, upstream: str, strip_prefix: str = ""):
        self.upstream = upstream.rstrip("/")
        self.strip_prefix = strip_prefix
        self._client: Optional[object] = None  # lazy httpx.AsyncClient

    async def _get_client(self):
        if self._client is None:
            try:
                import httpx
            except ImportError:
                raise ImportError(
                    "httpx is required for process/external mode proxying. "
                    "Install it with:  pip install enlace[process]"
                ) from None
            self._client = httpx.AsyncClient(
                base_url=self.upstream,
                timeout=60.0,
                follow_redirects=False,
            )
        return self._client

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # WebSocket proxying deferred to a future release
            await _send_error(send, 501, b"WebSocket proxying not yet supported")
            return

        client = await self._get_client()

        # Build the upstream path
        path = scope.get("path", "/")
        if self.strip_prefix and path.startswith(self.strip_prefix):
            path = path[len(self.strip_prefix) :] or "/"

        query = scope.get("query_string", b"")
        url = path
        if query:
            url = f"{path}?{query.decode('latin-1')}"

        # Read request body
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break

        # Forward headers (skip hop-by-hop)
        headers = {}
        for key, value in scope.get("headers", []):
            name = key.decode("latin-1").lower()
            if name in ("host", "transfer-encoding", "connection"):
                continue
            headers[name] = value.decode("latin-1")

        import httpx

        request = httpx.Request(
            method=scope["method"],
            url=url,
            headers=headers,
            content=body,
        )

        try:
            response = await client.send(request, stream=True)
        except Exception:
            await _send_error(send, 502, b"Bad Gateway: upstream unavailable")
            return

        # Stream response back
        try:
            response_headers = [
                (k.encode("latin-1"), v.encode("latin-1"))
                for k, v in response.headers.multi_items()
                if k.lower() not in ("transfer-encoding", "connection", "keep-alive")
            ]

            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": response_headers,
                }
            )

            async for chunk in response.aiter_bytes():
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    }
                )

            await send(
                {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False,
                }
            )
        finally:
            await response.aclose()


async def _send_error(send, status: int, body: bytes) -> None:
    """Send a simple error response."""
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        }
    )
