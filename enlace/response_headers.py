"""Per-app response headers declared in ``app.toml``; cross-origin isolation in a line.

An app names headers for sub-paths of itself, and enlace adds them to every response
on those paths, whoever produced it (the app's frontend, its API, the sign-in gate)::

    cross_origin_isolated = ["lab"]   # COOP + COEP + CORP on /{name}/lab/...

    [response_headers]
    "lab" = { "Cross-Origin-Embedder-Policy" = "credentialless" }  # overrides one
    "/" = { "X-Robots-Tag" = "noindex" }                           # the whole app

Both are off by default: an app that declares neither gets no header from here, and
the middleware is not even installed when no app declares one.

**Paths.** A key is a sub-path of the app, ``"/"`` (or ``""``) for all of it. It
applies under each of the app's mounts: the page at ``/{name}/`` and the API at its
``route_prefix`` (so ``CORP: same-origin`` also lands on ``{route_prefix}/lab/``,
harmless for same-origin fetches). Matching is by whole segments: ``"lab"`` covers
``/{name}/lab`` and ``/{name}/lab/...``, never ``/{name}/labels``. A request belongs
to the app with the longest mount covering it, and only that app's keys apply, so
one app's declaration never reaches another app's paths. When several of its keys
match, the longest wins header by header. A declared header replaces one of the same
name already on the response. Headers that would corrupt the response or act on the
whole origin (``Set-Cookie``, framing headers, ``Clear-Site-Data``,
``Strict-Transport-Security``...) are refused: see :data:`FORBIDDEN_HEADERS`.

**Cross-origin isolation.** ``cross_origin_isolated = true`` (the whole app) or a
list of sub-paths sends :data:`CROSS_ORIGIN_ISOLATION_HEADERS`. A page served with
them gets ``crossOriginIsolated === true``, hence ``SharedArrayBuffer`` (threaded
WebAssembly, memory shared with an ``AudioWorklet``) and precise timers. The price,
on every isolated page: a cross-origin resource loads only if it opts in (CORS, or
``Cross-Origin-Resource-Policy: cross-origin``), so third-party embeds (YouTube and
other iframes, analytics and ad scripts, images and fonts from a CDN that sends no
CORP) stop loading; a window it opens, or that opened it, loses its handle to it
unless same-origin (OAuth and payment pop-ups that report back through
``window.opener``); and ``CORP: same-origin`` stops other sites embedding its files.
Keep it on the one route that needs it, and mind three consequences:

- **Workers.** A dedicated worker started by an isolated page must have its *script*
  served with a compatible COEP, or it does not start at all. Put worker scripts
  (and the scripts that spawn threads, such as onnxruntime-web's ``.mjs``) under the
  isolated sub-path. Same-origin page scripts, wasm and data elsewhere need nothing.
- **Navigation.** Isolation is fixed when a document loads: a client-side route
  change into the isolated path does not isolate the page; link to it with a full
  navigation.
- **SPA fallback.** An unknown path under the isolated sub-path falls back to the
  app's root ``index.html``, served isolated; give the isolated page its own
  ``index.html`` and link to that.

The three-callable middleware is pure ASGI, as every enlace middleware is.
"""

import logging
import re
from collections.abc import Mapping, Sequence

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: What ``cross_origin_isolated`` sends. ``require-corp`` rather than ``credentialless``
#: because Safari supports only the former; an app can override it per sub-path.
CROSS_ORIGIN_ISOLATION_HEADERS: dict[str, str] = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
    "Cross-Origin-Resource-Policy": "same-origin",
}

#: Header names an app may not declare (lower-case): replacing them would corrupt the
#: response (framing, encoding, type), clobber the platform's session and CSRF
#: cookies, or act on the whole origin, i.e. on every other app.
FORBIDDEN_HEADERS: frozenset[str] = frozenset(
    {
        # framing and representation
        "connection",
        "content-encoding",
        "content-length",
        "content-range",
        "content-type",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        # the platform's cookies and the gate's answers
        "set-cookie",
        "location",
        "www-authenticate",
        # origin-wide effects
        "clear-site-data",
        "strict-transport-security",
        "service-worker-allowed",
        "nel",
        "report-to",
        "reporting-endpoints",
        "origin-agent-cluster",
        "accept-ch",
        "critical-ch",
    }
)

#: Header-name prefixes an app may not declare (lower-case). ``access-control-*`` is
#: the platform's CORS answer: this middleware wraps CORS, so a declared value would
#: replace it, e.g. pairing a chosen origin with ``Allow-Credentials`` on the app's API.
FORBIDDEN_HEADER_PREFIXES: tuple[str, ...] = ("access-control-",)

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # RFC 9110 field-name
_VALUE = re.compile(r"[\t\x20-\x7e]*")  # visible ASCII, space and tab
_BAD_PATH = re.compile(r"[?#%*\\\s]")


def normalize_subpath(key: str) -> str:
    """A sub-path key made canonical: no slash at either end, ``""`` for the whole app.

    >>> normalize_subpath("/lab/"), normalize_subpath("lab"), normalize_subpath("/")
    ('lab', 'lab', '')
    """
    if not isinstance(key, str):
        raise ValueError(f"a response-headers path must be a string, not {key!r}")
    sub = key.strip("/")
    segments = sub.split("/") if sub else []
    if _BAD_PATH.search(sub) or any(s in ("", ".", "..") for s in segments):
        raise ValueError(
            f"bad response-headers path {key!r}: use a plain sub-path of the app "
            "such as 'lab' or 'lab/models', or '/' for the whole app"
        )
    return sub


def _validate_headers(where: str, headers) -> dict[str, str]:
    if not isinstance(headers, Mapping) or not headers:
        raise ValueError(
            f"[response_headers] {where!r} must be a non-empty table of header = value"
        )
    seen: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not _TOKEN.fullmatch(name):
            raise ValueError(f"[response_headers] {where!r}: bad header name {name!r}")
        if name.lower() in FORBIDDEN_HEADERS or name.lower().startswith(
            FORBIDDEN_HEADER_PREFIXES
        ):
            raise ValueError(
                f"[response_headers] {where!r}: {name} cannot be declared (it would "
                "corrupt the response, the platform's cookies, or other apps)"
            )
        if not isinstance(value, str) or not _VALUE.fullmatch(value):
            raise ValueError(
                f"[response_headers] {where!r}: {name} needs a one-line ASCII value"
            )
        if name.lower() in seen:
            raise ValueError(
                f"[response_headers] {where!r}: {name!r} is declared twice "
                f"(also as {seen[name.lower()]!r})"
            )
        seen[name.lower()] = name
    return dict(headers)


def validate_response_headers(table) -> dict[str, dict[str, str]]:
    """Check an app.toml ``[response_headers]`` table; keys come back normalised.

    >>> validate_response_headers({"/lab/": {"X-A": "1"}})
    {'lab': {'X-A': '1'}}
    """
    if not isinstance(table, Mapping):
        raise ValueError(
            "[response_headers] must be a table of 'sub-path' = {header = value}"
        )
    out: dict[str, dict[str, str]] = {}
    for key, headers in table.items():
        sub = normalize_subpath(key)
        if sub in out:
            raise ValueError(
                f"[response_headers] declares the path {sub or '/'!r} twice"
            )
        out[sub] = _validate_headers(key, headers)
    return out


def validate_cross_origin_isolated(value) -> bool | list[str]:
    """Check ``cross_origin_isolated``: a bool, or a list of sub-paths (normalised).

    >>> validate_cross_origin_isolated(["/lab/"])
    ['lab']
    """
    if isinstance(value, bool):
        return value
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            "cross_origin_isolated must be true, false, or a list of sub-paths "
            f"(e.g. ['lab']), not {value!r}"
        )
    subs = [normalize_subpath(v) for v in value]
    if len(set(subs)) != len(subs):
        raise ValueError(f"cross_origin_isolated lists a path twice: {list(value)!r}")
    return subs


def merge_headers(*layers: Mapping[str, str]) -> dict[str, str]:
    """Merge header dicts, later layers winning, names compared case-insensitively.

    >>> merge_headers({"X-A": "1", "X-B": "2"}, {"x-a": "3"})
    {'X-B': '2', 'x-a': '3'}
    """
    out: dict[str, tuple[str, str]] = {}
    for layer in layers:
        for name, value in layer.items():
            out.pop(name.lower(), None)
            out[name.lower()] = (name, value)
    return dict(out.values())


def app_header_table(app) -> dict[str, dict[str, str]]:
    """Sub-path -> headers for an app: isolation, then ``[response_headers]`` on top."""
    isolated = app.cross_origin_isolated
    subs = [""] if isolated is True else list(isolated or [])
    table = {sub: dict(CROSS_ORIGIN_ISOLATION_HEADERS) for sub in subs}
    for sub, headers in app.response_headers.items():
        table[sub] = merge_headers(table.get(sub, {}), headers)
    return table


def app_mounts(app) -> list[str]:
    """The path prefixes an app answers under: its page and its API, root excluded."""
    mounts = (f"/{app.name}", app.route_prefix or "")
    return [m for m in dict.fromkeys(m.rstrip("/") for m in mounts) if m]


def header_rules_for_app(app) -> list[tuple[str, str, dict[str, str]]]:
    """The app's headers as ``(mount, absolute path prefix, headers)`` rules."""
    table = app_header_table(app)
    if table and (app.route_prefix or "").rstrip("/") == "":
        logger.warning(
            "app %r declares response headers but its route is the site root; "
            "they apply under /%s only, never to the whole site",
            app.name,
            app.name,
        )
    return [
        (mount, f"{mount}/{sub}" if sub else mount, headers)
        for mount in app_mounts(app)
        for sub, headers in table.items()
    ]


def path_under(prefix: str, path: str) -> bool:
    """Whether *path* is *prefix* or below it, by whole segments.

    >>> path_under("/a/lab", "/a/lab/x"), path_under("/a/lab", "/a/labels")
    (True, False)
    """
    return path == prefix or path.startswith(prefix + "/")


class ResponseHeadersMiddleware:
    """Pure-ASGI middleware adding declared headers to responses under path prefixes.

    *rules* are ``(mount, prefix, headers)`` triples on full request paths. A request
    is owned by the longest of *mounts* covering it (every app's mounts, so an app
    with a shorter mount never reaches a longer one's paths); only the rules of that
    mount apply, longer prefixes last, so the most specific wins a header both
    declare. Websockets and lifespan pass through untouched.

    >>> import asyncio
    >>> async def app(scope, receive, send):
    ...     await send({"type": "http.response.start", "status": 200, "headers": []})
    ...     await send({"type": "http.response.body", "body": b""})
    >>> mw = ResponseHeadersMiddleware(app, rules=[("/a", "/a", {"X-A": "1"})])
    >>> sent = []
    >>> async def send(m): sent.append(m)
    >>> asyncio.run(mw({"type": "http", "path": "/a/b"}, None, send))
    >>> sent[0]["headers"]
    [(b'x-a', b'1')]
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        rules: Sequence[tuple[str, str, Mapping[str, str]]],
        mounts: Sequence[str] = (),
    ):
        self.app = app
        self.rules = sorted(
            ((m.rstrip("/"), p.rstrip("/"), dict(h)) for m, p, h in rules),
            key=lambda rule: len(rule[1]),
        )
        all_mounts = {m.rstrip("/") for m in mounts} | {m for m, _, _ in self.rules}
        self.mounts = sorted((m for m in all_mounts if m), key=len, reverse=True)

    def headers_for(self, path: str) -> dict[str, str]:
        """The headers a response to *path* gets (empty when no rule covers it)."""
        owner = next((m for m in self.mounts if path_under(m, path)), None)
        return merge_headers(
            *(h for m, p, h in self.rules if m == owner and path_under(p, path))
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        is_http = scope["type"] == "http"
        extra = self.headers_for(scope.get("path", "")) if is_http else {}
        if not extra:
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers", []))}
                headers = MutableHeaders(scope=message)
                for name, value in extra.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


def add_response_headers(parent, config) -> bool:
    """Install :class:`ResponseHeadersMiddleware` on *parent* if an app declares any."""
    rules = [rule for app in config.apps for rule in header_rules_for_app(app)]
    if not rules:
        return False
    mounts = [m for app in config.apps for m in app_mounts(app)]
    parent.add_middleware(ResponseHeadersMiddleware, rules=rules, mounts=mounts)
    return True
