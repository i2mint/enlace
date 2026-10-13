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

import re
from typing import Callable, Iterable, Optional
from urllib.parse import quote, unquote

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
    reads only the one key that renames a platform cookie.

    >>> from types import SimpleNamespace
    >>> platform_cookie_names(SimpleNamespace(auth={"session_cookie_name": "s"}))[-1]
    's'
    """
    auth = getattr(platform, "auth", None) or {}
    custom = auth.get("session_cookie_name") if isinstance(auth, dict) else None
    return PLATFORM_COOKIE_NAMES + ((custom,) if custom else ())


def platform_proxy_filters(platform=None, *, forward_credentials: bool = False) -> dict:
    """:func:`make_proxy_app` keyword arguments that keep platform credentials home.

    The one policy for every proxied app, whatever runs it (an external server,
    a local process, a container from ``enlace_docker``): the upstream does not
    see the visitor's platform cookies or ``X-CSRF-Token`` unless the caller
    passes *forward_credentials* (process and docker modes pass an app's
    ``forward_platform_credentials``; external mode never does), and it may
    never set a platform cookie or send :data:`PLATFORM_UNSAFE_RESPONSE_HEADERS`,
    whatever it is granted.

    >>> filters = platform_proxy_filters()
    >>> filters["cookie_filter"]("enlace_session"), filters["cookie_filter"]("theme")
    (False, True)
    >>> platform_proxy_filters(forward_credentials=True)["cookie_filter"] is None
    True
    """
    platform_cookies = platform_cookie_filter(names=platform_cookie_names(platform))
    return dict(
        cookie_filter=None if forward_credentials else platform_cookies,
        set_cookie_filter=platform_cookies,
        drop_request_headers=(
            () if forward_credentials else PLATFORM_CREDENTIAL_REQUEST_HEADERS
        ),
        drop_response_headers=PLATFORM_UNSAFE_RESPONSE_HEADERS,
    )


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


#: A path as it may travel on the wire: RFC 3986 ``pchar`` and ``/``, with every
#: ``%`` starting a two-digit escape. Anything else is not forwarded verbatim.
_WIRE_PATH = re.compile(r"(?:[A-Za-z0-9\-._~!$&'()*+,;=:@/]|%[0-9A-Fa-f]{2})*")

#: Characters :func:`quote` leaves alone when re-encoding a decoded path:
#: ``pchar`` minus ``%``, so ``%``, ``?``, ``#`` and spaces are escaped.
_PATH_SAFE = "/!$&'()*+,;=:@-._~"


def _upstream_path(scope, strip_prefix: str = "") -> str:
    """The path to forward, percent-encoded as the client sent it (enlace#74).

    ASGI servers hand ``scope["path"]`` over *decoded*, so building the upstream
    URL from it changes the request: ``a%2Fb`` arrives as ``a/b``, ``a%3Fb``
    moves ``b`` into the query, and ``100%25`` becomes a bare ``%``. The bytes
    the client sent are in ``scope["raw_path"]``, so the remainder after the
    mount prefix is taken from there. That happens only when the raw path
    provably *is* the decoded one: its leading segments decode to exactly
    ``strip_prefix``, the rest decodes to exactly the decoded remainder, and it
    is well-formed on the wire. Otherwise the decoded remainder is re-quoted,
    which keeps ``%``, ``?`` and ``#`` inside the path.

    A remainder with a ``.`` or ``..`` segment (decoded, so ``%2E%2E`` counts)
    is also re-quoted. httpx then resolves the dot segments, as it always
    did, rather than handing an escaped ``..`` to the upstream to decode.

    The result always starts with ``/``. Concatenated onto the upstream's base
    URL it can only name a path on that host, never a different authority
    (``/api/x@evil`` must not become ``http://127.0.0.1:9100@evil``).

    >>> scope = {"path": "/api/x/a/b", "raw_path": b"/api/x/a%2Fb"}
    >>> _upstream_path(scope, "/api/x")
    '/a%2Fb'
    >>> _upstream_path({"path": "/api/x/100%"}, "/api/x")
    '/100%25'
    >>> _upstream_path({"path": "/api/x/../y", "raw_path": b"/api/x/%2E%2E/y"}, "/api/x")
    '/../y'
    """
    path = scope.get("path") or "/"
    prefix = strip_prefix.rstrip("/")
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        remainder = path[len(prefix) :]
    else:  # not under the prefix segment-wise (no Mount routes this): forward as is
        remainder, prefix = path, ""
    if not remainder.startswith("/"):
        remainder = "/" + remainder

    if any(seg in (".", "..") for seg in remainder.split("/")):
        return quote(remainder, safe=_PATH_SAFE)

    raw = scope.get("raw_path")
    if isinstance(raw, (bytes, bytearray)):
        raw_s = bytes(raw).decode("latin-1")
        n = len(prefix.split("/")) if prefix else 1
        parts = raw_s.split("/")
        if [unquote(p) for p in parts[:n]] == (prefix.split("/") if prefix else [""]):
            raw_rest = "/" + "/".join(parts[n:])
            # unquote's default errors="replace" is how uvicorn decodes too, so
            # an escape that is not UTF-8 (%FF) still matches and travels as is.
            if _WIRE_PATH.fullmatch(raw_rest) and unquote(raw_rest) == remainder:
                return raw_rest
    return quote(remainder, safe=_PATH_SAFE)


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
            to the client. :func:`platform_proxy_filters` gives all four
            filters as every enlace strategy applies them.

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

        # The upstream path, as the client encoded it (see _upstream_path).
        path = _upstream_path(scope, self.strip_prefix)

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
