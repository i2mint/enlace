# enlace.proxy

Lightweight ASGI reverse proxy for process and external backends.

Forwards HTTP requests to an upstream server, stripping the mount prefix
from the path.  Uses `httpx` when available, falling back to a stdlib
implementation for simple cases.

This module is lazy-loaded: it only imports `httpx` when a proxy ASGI
app is actually instantiated, so the dependency remains optional.

### Functions

| [`make_proxy_app`](#enlace.proxy.make_proxy_app)(\*, upstream[, strip_prefix])   | Create an ASGI app that proxies requests to *upstream*.   |
|-------------------------------------------------------------------------------------------------|-----------------------------------------------------------|

### enlace.proxy.make_proxy_app(, upstream, strip_prefix='')

Create an ASGI app that proxies requests to *upstream*.

* **Parameters:**
  * **upstream** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – Base URL of the upstream server (e.g. `http://127.0.0.1:9100`).
  * **strip_prefix** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – Route prefix to strip before forwarding
    (e.g. `/api/blog` → upstream receives `/`).
* **Returns:**
  An ASGI callable.
