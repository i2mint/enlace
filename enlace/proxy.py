"""Lightweight ASGI reverse proxy for process and external backends.

Forwards HTTP requests to an upstream server, stripping the mount prefix
from the path.  Uses ``httpx`` when available, falling back to a stdlib
implementation for simple cases.

This module is lazy-loaded: it only imports ``httpx`` when a proxy ASGI
app is actually instantiated, so the dependency remains optional.

What the credential filters do and do not contain: they keep the platform's
cookies and CSRF header away from an upstream *server*, and keep an upstream
from setting platform cookies or sending origin-wide headers
(``PLATFORM_UNSAFE_RESPONSE_HEADERS``). They do not try to stop an upstream
from *navigating* a visitor elsewhere on the origin: a ``Location`` or
``Refresh`` pointing at ``/auth/...`` is passed through. A redirect is only a
top-level GET navigation, which any page on any site can already cause (with
the visitor's SameSite=Lax cookies attached), and a proxied app's own pages are
served on the platform origin, one ``location.href = ...`` away from the same
thing. So the platform's GET routes must be safe to navigate to on their own
(the POST ones need CSRF); filtering these headers would only look like a
boundary.
"""

from typing import Callable, Iterable, Optional

#: Cookies the platform itself sets on its own origin -- enlace_auth's defaults
#: (``AuthConfig.session_cookie_name``, ``CSRFMiddleware`` cookie name, the
#: social-login ``enlace_oauth_state`` cookie, and the per-app
#: ``shared_auth_<app>`` cookies). Keep in sync with enlace_auth. They
#: are credentials for *this* platform and must never reach an upstream that
#: is not part of it.
PLATFORM_COOKIE_NAMES = ("enlace_session", "enlace_csrf", "enlace_oauth_state")
PLATFORM_COOKIE_PREFIXES = ("shared_auth_",)

#: Request headers carrying platform credentials, withheld from proxied
#: upstreams (the CSRF double-submit token pairs with ``enlace_csrf``).
PLATFORM_CREDENTIAL_REQUEST_HEADERS = ("x-csrf-token",)

#: Response headers a proxied upstream may not send on the platform origin:
#: ``Clear-Site-Data`` could wipe the platform's cookies/storage, and
#: ``Service-Worker-Allowed`` could let a script under the app's prefix
#: register a service worker controlling the whole origin.
PLATFORM_UNSAFE_RESPONSE_HEADERS = ("clear-site-data", "service-worker-allowed")

# Original names, kept for importers: external mode was the first user.
EXTERNAL_DROP_REQUEST_HEADERS = PLATFORM_CREDENTIAL_REQUEST_HEADERS
EXTERNAL_DROP_RESPONSE_HEADERS = PLATFORM_UNSAFE_RESPONSE_HEADERS

CookieFilter = Callable[[str], bool]  # cookie name -> forward it?


def platform_cookie_filter(
    *,
    names: Iterable[str] = PLATFORM_COOKIE_NAMES,
    prefixes: Iterable[str] = PLATFORM_COOKIE_PREFIXES,
) -> CookieFilter:
    """Return a filter that keeps every cookie except the platform's own.

    Used for every proxied app (``mode="external"`` and ``mode="process"``): an
    upstream has no business seeing a visitor's platform session, and must not
    be able to set (overwrite) one on the platform's origin either.

    Names match case-insensitively. Browsers and Starlette keep cookie names
    case-sensitive, so ``Enlace_Session`` is a different cookie today, but a
    case-variant of a platform name has no legitimate use and would become a
    shadow the day any reader matched names loosely.

    >>> keep = platform_cookie_filter()
    >>> [keep(n) for n in ("theme", "Enlace_Session", "SHARED_AUTH_x")]
    [True, False, False]
    """
    names = frozenset(n.casefold() for n in names)
    prefixes = tuple(p.casefold() for p in prefixes)

    def keep(name: str) -> bool:
        folded = name.casefold()
        return not (folded in names or folded.startswith(prefixes))

    return keep


def platform_cookie_names(platform=None) -> tuple[str, ...]:
    """The platform's own cookie names, honouring a custom session cookie name.

    ``[auth]`` is opaque to enlace core (enlace_auth owns its schema), so this
    reads only the one key that renames a platform cookie,
    ``[auth] session_cookie_name``.

    >>> platform_cookie_names()
    ('enlace_session', 'enlace_csrf', 'enlace_oauth_state')
    >>> from types import SimpleNamespace
    >>> platform_cookie_names(SimpleNamespace(auth={"session_cookie_name": "s"}))[-1]
    's'
    """
    auth = getattr(platform, "auth", None) or {}
    custom = auth.get("session_cookie_name") if isinstance(auth, dict) else None
    return PLATFORM_COOKIE_NAMES + ((custom,) if custom else ())


def platform_proxy_filters(
    *, platform=None, forward_credentials: bool = False
) -> dict:
    """``make_proxy_app`` kwargs that keep platform credentials inside the platform.

    The one policy every proxying backend uses (enlace's process and external
    modes, and plugin strategies such as enlace_docker): the upstream never
    receives the visitor's platform cookies or CSRF header, and never sets
    platform cookies or origin-wide headers. ``forward_credentials=True`` (an
    app's ``forward_platform_credentials`` opt-in) lets the upstream *read* the
    platform session; it still cannot set it.

    Usage: ``make_proxy_app(upstream=..., strip_prefix=...,
    **platform_proxy_filters(platform=platform))``.

    >>> f = platform_proxy_filters()
    >>> sorted(f)  # doctest: +NORMALIZE_WHITESPACE
    ['cookie_filter', 'drop_request_headers',
     'drop_response_headers', 'set_cookie_filter']
    >>> f["cookie_filter"]("enlace_session"), f["set_cookie_filter"]("theme")
    (False, True)
    >>> platform_proxy_filters(forward_credentials=True)["cookie_filter"] is None
    True
    """
    platform_cookies = platform_cookie_filter(names=platform_cookie_names(platform))
    return {
        "cookie_filter": None if forward_credentials else platform_cookies,
        "set_cookie_filter": platform_cookies,
        "drop_request_headers": (
            () if forward_credentials else PLATFORM_CREDENTIAL_REQUEST_HEADERS
        ),
        "drop_response_headers": PLATFORM_UNSAFE_RESPONSE_HEADERS,
    }


def _filter_cookie_header(value: str, keep: CookieFilter) -> str:
    """Drop the cookies *keep* rejects from a request ``Cookie`` header value."""
    parts = []
    for part in value.split(";"):
        name = part.split("=", 1)[0].strip()
        if name and keep(name):
            parts.append(part.strip())
    return "; ".join(parts)


def _set_cookie_allowed(value: str, keep: CookieFilter) -> bool:
    """Whether a ``Set-Cookie`` header value may pass *keep*.

    A nameless cookie (``=enlace_session=x`` or a bare value) is refused
    outright: browsers send such a cookie as its bare value, so
    ``=enlace_session=x`` reaches the server as ``enlace_session=x`` and would
    shadow the real one.
    """
    pair = value.split(";", 1)[0]
    if "=" not in pair:
        return False
    name = pair.split("=", 1)[0].strip()
    return bool(name) and keep(name)


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


def make_proxy_app(
    *,
    upstream: str,
    strip_prefix: str = "",
    cookie_filter: Optional[CookieFilter] = None,
    set_cookie_filter: Optional[CookieFilter] = None,
    drop_request_headers: Iterable[str] = (),
    drop_response_headers: Iterable[str] = (),
):
    """Create an ASGI app that proxies requests to *upstream*.

    Args:
        upstream: Base URL of the upstream server (e.g. ``http://127.0.0.1:9100``).
        strip_prefix: Route prefix to strip before forwarding
            (e.g. ``/api/blog`` → upstream receives ``/``).
        cookie_filter: ``name -> bool``; when given, request cookies it
            rejects are not forwarded and upstream ``Set-Cookie`` headers
            naming them are dropped. ``None`` (the default) forwards all
            cookies. See :func:`platform_cookie_filter`.
        set_cookie_filter: the filter for upstream ``Set-Cookie`` alone, when
            it must differ from *cookie_filter* (an app trusted to read the
            platform session is still not allowed to set it). Defaults to
            *cookie_filter*.
        drop_request_headers / drop_response_headers: header names
            (case-insensitive) never forwarded upstream / never passed back
            to the client. See ``EXTERNAL_DROP_*`` for what external apps use.

    Returns:
        An ASGI callable.
    """
    return _HttpxProxy(
        upstream=upstream,
        strip_prefix=strip_prefix,
        cookie_filter=cookie_filter,
        set_cookie_filter=set_cookie_filter,
        drop_request_headers=drop_request_headers,
        drop_response_headers=drop_response_headers,
    )


class _HttpxProxy:
    """Pure-ASGI reverse proxy backed by ``httpx.AsyncClient``."""

    def __init__(
        self,
        *,
        upstream: str,
        strip_prefix: str = "",
        timeout: float = _DEFAULT_TIMEOUT_S,
        cookie_filter: Optional[CookieFilter] = None,
        set_cookie_filter: Optional[CookieFilter] = None,
        drop_request_headers: Iterable[str] = (),
        drop_response_headers: Iterable[str] = (),
    ):
        self.cookie_filter = cookie_filter
        self.set_cookie_filter = (
            cookie_filter if set_cookie_filter is None else set_cookie_filter
        )
        self._drop_request = {"host", "transfer-encoding", "connection"} | {
            h.lower() for h in drop_request_headers
        }
        self._drop_response = {"transfer-encoding", "connection", "keep-alive"} | {
            h.lower() for h in drop_response_headers
        }
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
            if name in self._drop_request:
                continue
            decoded = value.decode("latin-1")
            if name == "cookie" and self.cookie_filter is not None:
                decoded = _filter_cookie_header(decoded, self.cookie_filter)
                if not decoded:
                    continue
            if name in headers:
                # A repeated field is one field (RFC 9110 §5.3); cookies are
                # joined with "; " because HTTP/2 splits them (RFC 9113 §8.2.3).
                sep = "; " if name == "cookie" else ", "
                decoded = f"{headers[name]}{sep}{decoded}"
            headers[name] = decoded

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
                if k.lower() not in self._drop_response
                and not (
                    k.lower() == "set-cookie"
                    and self.set_cookie_filter is not None
                    and not _set_cookie_allowed(v, self.set_cookie_filter)
                )
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
