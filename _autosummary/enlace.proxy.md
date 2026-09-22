# enlace.proxy

Lightweight ASGI reverse proxy for process and external backends.

Forwards HTTP requests to an upstream server, stripping the mount prefix
from the path.  Uses `httpx` when available, falling back to a stdlib
implementation for simple cases.

This module is lazy-loaded: it only imports `httpx` when a proxy ASGI
app is actually instantiated, so the dependency remains optional.

### Module Attributes

| [`PLATFORM_COOKIE_NAMES`](#enlace.proxy.PLATFORM_COOKIE_NAMES)          | Cookies the platform itself sets on its own origin -- enlace_auth's defaults (`AuthConfig.session_cookie_name`, `CSRFMiddleware` cookie name, and the per-app `shared_auth_<app>` cookies).                                                                            |
|---------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`EXTERNAL_DROP_REQUEST_HEADERS`](#enlace.proxy.EXTERNAL_DROP_REQUEST_HEADERS)  | Request headers carrying platform credentials, withheld from external upstreams (the CSRF double-submit token pairs with `enlace_csrf`).                                                                                                                               |
| [`EXTERNAL_DROP_RESPONSE_HEADERS`](#enlace.proxy.EXTERNAL_DROP_RESPONSE_HEADERS) | Response headers an external upstream may not send on the platform origin: `Clear-Site-Data` could wipe the platform's cookies/storage, and `Service-Worker-Allowed` could let a script under the app's prefix register a service worker controlling the whole origin. |

### Functions

| [`make_proxy_app`](#enlace.proxy.make_proxy_app)(\*, upstream[, strip_prefix, ...])   | Create an ASGI app that proxies requests to *upstream*.            |
|------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------|
| [`platform_cookie_filter`](#enlace.proxy.platform_cookie_filter)(\*[, names, prefixes])       | Return a filter that keeps every cookie except the platform's own. |

### enlace.proxy.EXTERNAL_DROP_REQUEST_HEADERS *= ('x-csrf-token',)*

Request headers carrying platform credentials, withheld from external
upstreams (the CSRF double-submit token pairs with `enlace_csrf`).

### enlace.proxy.EXTERNAL_DROP_RESPONSE_HEADERS *= ('clear-site-data', 'service-worker-allowed')*

Response headers an external upstream may not send on the platform origin:
`Clear-Site-Data` could wipe the platform’s cookies/storage, and
`Service-Worker-Allowed` could let a script under the app’s prefix
register a service worker controlling the whole origin.

### enlace.proxy.PLATFORM_COOKIE_NAMES *= ('enlace_session', 'enlace_csrf')*

Cookies the platform itself sets on its own origin – enlace_auth’s defaults
(`AuthConfig.session_cookie_name`, `CSRFMiddleware` cookie name, and the
per-app `shared_auth_<app>` cookies). Keep in sync with enlace_auth. They
are credentials for *this* platform and must never reach an upstream that
is not part of it.

### enlace.proxy.make_proxy_app(, upstream, strip_prefix='', cookie_filter=None, drop_request_headers=(), drop_response_headers=())

Create an ASGI app that proxies requests to *upstream*.

* **Parameters:**
  * **upstream** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – Base URL of the upstream server (e.g. `http://127.0.0.1:9100`).
  * **strip_prefix** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – Route prefix to strip before forwarding
    (e.g. `/api/blog` → upstream receives `/`).
  * **cookie_filter** ([`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Callable`](https://docs.python.org/3/library/typing.html#typing.Callable)[[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)], [`bool`](https://docs.python.org/3/builtins/functions.html#bool)]]) – `name -> bool`; when given, request cookies it
    rejects are not forwarded and upstream `Set-Cookie` headers
    naming them are dropped. `None` (the default) forwards all
    cookies, which suits a local process app that is part of the
    platform. See [`platform_cookie_filter()`](#enlace.proxy.platform_cookie_filter).
  * **drop_response_headers** ([`Iterable`](https://docs.python.org/3/library/typing.html#typing.Iterable)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]) – header names
    (case-insensitive) never forwarded upstream / never passed back
    to the client. See `EXTERNAL_DROP_*` for what external apps use.
* **Returns:**
  An ASGI callable.

### enlace.proxy.platform_cookie_filter(, names=('enlace_session', 'enlace_csrf'), prefixes=('shared_auth_',))

Return a filter that keeps every cookie except the platform’s own.

Used for `mode="external"` apps: an upstream on another host has no
business seeing a visitor’s platform session, and must not be able to set
(overwrite) one on the platform’s origin either.

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/typing.html#typing.Callable)[[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)], [`bool`](https://docs.python.org/3/builtins/functions.html#bool)]
