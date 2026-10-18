# enlace.proxy

Lightweight ASGI reverse proxy for process and external backends.

Forwards HTTP requests to an upstream server, stripping the mount prefix
from the path.  Uses `httpx` when available, falling back to a stdlib
implementation for simple cases.

This module is lazy-loaded: it only imports `httpx` when a proxy ASGI
app is actually instantiated, so the dependency remains optional.

What the credential filters do and do not contain: they keep the platform’s
cookies and CSRF header away from an upstream *server*, and keep an upstream
from setting platform cookies or sending origin-wide headers
(`PLATFORM_UNSAFE_RESPONSE_HEADERS`). They do not try to stop an upstream
from *navigating* a visitor elsewhere on the origin: a `Location` or
`Refresh` pointing at `/auth/...` is passed through. A redirect is only a
top-level GET navigation, which any page on any site can already cause (with
the visitor’s SameSite=Lax cookies attached), and a proxied app’s own pages are
served on the platform origin, one `location.href = ...` away from the same
thing. So the platform’s GET routes must be safe to navigate to on their own
(the POST ones need CSRF); filtering these headers would only look like a
boundary.

### Module Attributes

| [`PLATFORM_COOKIE_NAMES`](#enlace.proxy.PLATFORM_COOKIE_NAMES)               | Cookies the platform itself sets on its own origin -- enlace_auth's defaults (`AuthConfig.session_cookie_name`, `CSRFMiddleware` cookie name, the social-login `enlace_oauth_state` cookie, and the per-app `shared_auth_<app>` cookies).                            |
|--------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`PLATFORM_CREDENTIAL_REQUEST_HEADERS`](#enlace.proxy.PLATFORM_CREDENTIAL_REQUEST_HEADERS) | Request headers carrying platform credentials, withheld from proxied upstreams (the CSRF double-submit token pairs with `enlace_csrf`).                                                                                                                              |
| [`PLATFORM_UNSAFE_RESPONSE_HEADERS`](#enlace.proxy.PLATFORM_UNSAFE_RESPONSE_HEADERS)    | Response headers a proxied upstream may not send on the platform origin: `Clear-Site-Data` could wipe the platform's cookies/storage, and `Service-Worker-Allowed` could let a script under the app's prefix register a service worker controlling the whole origin. |

### Functions

| [`make_proxy_app`](#enlace.proxy.make_proxy_app)(\*, upstream[, strip_prefix, ...])   | Create an ASGI app that proxies requests to *upstream*.                                                                  |
|------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------|
| [`platform_cookie_filter`](#enlace.proxy.platform_cookie_filter)(\*[, names, prefixes])       | Return a filter that keeps every cookie except the platform's own.                                                       |
| [`platform_cookie_names`](#enlace.proxy.platform_cookie_names)([platform])                   | The platform's own cookie names, honouring a custom session cookie name.                                                 |
| [`platform_proxy_filters`](#enlace.proxy.platform_proxy_filters)([platform, ...])             | [`make_proxy_app()`](#enlace.proxy.make_proxy_app) keyword arguments that keep platform credentials home. |

### enlace.proxy.PLATFORM_COOKIE_NAMES *= ('enlace_session', 'enlace_csrf', 'enlace_oauth_state')*

Cookies the platform itself sets on its own origin – enlace_auth’s defaults
(`AuthConfig.session_cookie_name`, `CSRFMiddleware` cookie name, the
social-login `enlace_oauth_state` cookie, and the per-app
`shared_auth_<app>` cookies). Keep in sync with enlace_auth. They
are credentials for *this* platform and must never reach an upstream that
is not part of it.

### enlace.proxy.PLATFORM_CREDENTIAL_REQUEST_HEADERS *= ('x-csrf-token',)*

Request headers carrying platform credentials, withheld from proxied
upstreams (the CSRF double-submit token pairs with `enlace_csrf`).

### enlace.proxy.PLATFORM_UNSAFE_RESPONSE_HEADERS *= ('clear-site-data', 'service-worker-allowed')*

Response headers a proxied upstream may not send on the platform origin:
`Clear-Site-Data` could wipe the platform’s cookies/storage, and
`Service-Worker-Allowed` could let a script under the app’s prefix
register a service worker controlling the whole origin.

### enlace.proxy.make_proxy_app(, upstream, strip_prefix='', cookie_filter=None, set_cookie_filter=None, drop_request_headers=(), drop_response_headers=())

Create an ASGI app that proxies requests to *upstream*.

* **Parameters:**
  * **upstream** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – Base URL of the upstream server (e.g. `http://127.0.0.1:9100`).
  * **strip_prefix** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – Route prefix to strip before forwarding
    (e.g. `/api/blog` → upstream receives `/`).
  * **cookie_filter** ([`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Callable`](https://docs.python.org/3/library/typing.html#typing.Callable)[[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)], [`bool`](https://docs.python.org/3/builtins/functions.html#bool)]]) – `name -> bool`; when given, request cookies it
    rejects are not forwarded and upstream `Set-Cookie` headers
    naming them are dropped. `None` (the default) forwards all
    cookies. See [`platform_cookie_filter()`](#enlace.proxy.platform_cookie_filter).
  * **set_cookie_filter** ([`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Callable`](https://docs.python.org/3/library/typing.html#typing.Callable)[[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)], [`bool`](https://docs.python.org/3/builtins/functions.html#bool)]]) – the filter for upstream `Set-Cookie` alone, when
    it must differ from *cookie_filter* (an app trusted to read the
    platform session is still not allowed to set it). Defaults to
    *cookie_filter*.
  * **drop_response_headers** ([`Iterable`](https://docs.python.org/3/library/typing.html#typing.Iterable)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]) – header names
    (case-insensitive) never forwarded upstream / never passed back
    to the client. [`platform_proxy_filters()`](#enlace.proxy.platform_proxy_filters) gives all four
    filters as every enlace strategy applies them.
* **Returns:**
  An ASGI callable.

### enlace.proxy.platform_cookie_filter(, names=('enlace_session', 'enlace_csrf', 'enlace_oauth_state'), prefixes=('shared_auth_',))

Return a filter that keeps every cookie except the platform’s own.

Used for every proxied app (`mode="external"` and `mode="process"`): an
upstream has no business seeing a visitor’s platform session, and must not
be able to set (overwrite) one on the platform’s origin either.

Names match case-insensitively. Browsers and Starlette keep cookie names
case-sensitive, so `Enlace_Session` is a different cookie today, but a
case-variant of a platform name has no legitimate use and would become a
shadow the day any reader matched names loosely.

```pycon
>>> keep = platform_cookie_filter()
>>> [keep(n) for n in ("theme", "Enlace_Session", "SHARED_AUTH_x")]
[True, False, False]
```

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/typing.html#typing.Callable)[[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)], [`bool`](https://docs.python.org/3/builtins/functions.html#bool)]

### enlace.proxy.platform_cookie_names(platform=None)

The platform’s own cookie names, honouring a custom session cookie name.

`[auth]` is opaque to enlace core (enlace_auth owns its schema), so this
reads only the one key that renames a platform cookie.

```pycon
>>> from types import SimpleNamespace
>>> platform_cookie_names(SimpleNamespace(auth={"session_cookie_name": "s"}))[-1]
's'
```

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

### enlace.proxy.platform_proxy_filters(platform=None, , forward_credentials=False)

[`make_proxy_app()`](#enlace.proxy.make_proxy_app) keyword arguments that keep platform credentials home.

The one policy for every proxied app, whatever runs it (an external server,
a local process, a container from `enlace_docker`): the upstream does not
see the visitor’s platform cookies or `X-CSRF-Token` unless the caller
passes *forward_credentials* (process and docker modes pass an app’s
`forward_platform_credentials`; external mode never does), and it may
never set a platform cookie or send [`PLATFORM_UNSAFE_RESPONSE_HEADERS`](#enlace.proxy.PLATFORM_UNSAFE_RESPONSE_HEADERS),
whatever it is granted.

```pycon
>>> filters = platform_proxy_filters()
>>> filters["cookie_filter"]("enlace_session"), filters["cookie_filter"]("theme")
(False, True)
>>> platform_proxy_filters(forward_credentials=True)["cookie_filter"] is None
True
```

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)
