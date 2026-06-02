"""Lightweight ASGI reverse proxy for process and external backends.

Forwards HTTP requests to an upstream server, stripping the mount prefix
from the path.  Uses ``httpx`` when available, falling back to a stdlib
implementation for simple cases.

This module is lazy-loaded: it only imports ``httpx`` when a proxy ASGI
app is actually instantiated, so the dependency remains optional.
"""

from typing import Optional

# Default per-request timeout (seconds) for proxied requests. Bounds a hung
# upstream so a stuck app can't tie up the gateway indefinitely.
_DEFAULT_TIMEOUT_S = 60.0


def _request_timeout(accept: str, base: float) -> Optional[dict]:
    """Per-request httpx timeout override (for ``request.extensions['timeout']``).

    Long-lived streams must **not** be killed by the read timeout: a
    live-but-idle Server-Sent-Events stream sends nothing for stretches, so a
    finite read timeout would drop the connection after ``base`` seconds and
    push the client into an endless reconnect loop. ``EventSource`` always
    sends ``Accept: text/event-stream``; for those we disable only the read
    timeout (connect / write / pool stay bounded). Non-streaming requests get
    ``None`` here — they use the client's default bounded timeout.

    Returns a timeout dict in httpx's ``request.extensions['timeout']`` shape,
    or ``None`` to leave the client default in place.
    """
    if accept.lower().startswith("text/event-stream"):
        return {"connect": base, "read": None, "write": base, "pool": base}
    return None


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

    def __init__(
        self,
        *,
        upstream: str,
        strip_prefix: str = "",
        timeout: float = _DEFAULT_TIMEOUT_S,
    ):
        self.upstream = upstream.rstrip("/")
        self.strip_prefix = strip_prefix
        self.timeout = timeout
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
                timeout=self.timeout,
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
        # httpx.Request(url=...) does not apply AsyncClient.base_url (only the
        # convenience methods like client.get/post do). Construct the absolute
        # URL ourselves so requests reach the upstream regardless of scheme.
        url = f"{self.upstream}{path}"
        if query:
            url = f"{url}?{query.decode('latin-1')}"

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

        # SSE / streaming upstreams must not be cut off by the read timeout —
        # see _request_timeout. Override per-request so an idle event stream
        # stays open instead of dropping every `self.timeout` seconds.
        timeout_override = _request_timeout(headers.get("accept", ""), self.timeout)
        if timeout_override is not None:
            request.extensions["timeout"] = timeout_override

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
