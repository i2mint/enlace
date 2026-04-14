# ASGI composition and dynamic sub-application mounting in FastAPI/Starlette

**Mounting independent Python web applications under a single FastAPI process is fully supported through Starlette's `Mount` primitive, but the approach comes with sharp edges around middleware propagation, lifespan management, OpenAPI isolation, and dynamic registration that require deliberate architectural choices.** The core trade-off is between `include_router()` (shared everything, unified docs) and `mount()` (full isolation, independent apps). For a personal app platform aggregating 10–50 apps discovered from a config registry, the most practical pattern combines `mount()` for true ASGI sub-apps with a custom dispatcher layer for dynamic registration — and a `cascade_lifespan` workaround to propagate startup/shutdown events that Starlette deliberately does not forward to mounted sub-apps [1][10]. No production-grade library exists today for dynamic hot-mounting with unified OpenAPI merging; you will build this orchestration layer yourself atop well-understood primitives.

This report covers all twelve research questions with working code, known failure modes, and production guidance.

---

## 1. How `mount()` works internally and where it breaks

When you call `app.mount("/prefix", sub_app)`, Starlette creates a `Mount` object and appends it to `app.routes` [3]. On each request, the `Router` iterates this list and calls each route's `matches()` method. `Mount` performs **prefix matching** via a compiled regex, then modifies the ASGI scope before delegating:

```python
# What Mount does to the ASGI scope:
scope["root_path"] = original_root_path + "/prefix"
scope["path"] = "/remaining/path"       # prefix stripped
scope["app_root_path"] = original_root_path  # preserved for url_for()
```

The sub-app never sees the mount prefix — it receives requests as if it were the root application [1][7]. This is the ASGI spec's `root_path` mechanism working as designed [16].

**Middleware interaction** is the first critical subtlety. The parent app's middleware stack is built as `ServerErrorMiddleware → [user middleware] → ExceptionMiddleware → Router` [2]. Since the Router (containing all Mounts) sits *inside* the middleware chain, **all requests to mounted sub-apps pass through the parent's middleware** — including CORS, authentication, and logging middleware. However, a mounted `FastAPI()` instance also builds *its own* middleware stack internally. This means requests traverse two middleware chains: parent's, then sub-app's [2][38].

**Exception handlers defined on the parent do not propagate to mounted sub-apps.** Each FastAPI/Starlette instance has its own `ExceptionMiddleware` with its own handler registry. The sub-app's exception middleware catches errors before they could reach the parent's [29]. Similarly, **FastAPI's dependency injection does not cross mount boundaries** — `Depends()` declarations on the parent app are invisible to sub-apps [1][31].

**OpenAPI schemas are completely separate.** Mounted sub-app routes never appear in the parent's `/docs`. Each sub-app serves its own OpenAPI docs at `/{prefix}/docs` [1][22]. There is no built-in mechanism to merge schemas, though the `modular-monolith-fastapi` project demonstrates a custom script that walks all mounts to produce a unified spec [26].

**Known gotchas worth flagging:**

- **Mounting on `APIRouter` is broken.** Although `APIRouter` inherits `mount()` from Starlette's `Router`, routes mounted this way do not resolve correctly when the router is included via `include_router()`. This is confirmed in issues #4194 and #10180 [9]. Always mount on the top-level `FastAPI()` instance.
- **`BaseHTTPMiddleware` on the parent silently swallows exceptions from mounted sub-apps** — stack traces disappear from logs entirely. This has been reported, fixed, regressed, and remains problematic [8][37]. Use pure ASGI middleware instead.
- **Starlette 0.33.0 changed `root_path`/`path` handling** (PR #2352), breaking some mount configurations. FastAPI 0.109.2 restored compatibility [7].

---

## 2. `include_router()` vs `mount()` — a decision framework

These two composition mechanisms serve fundamentally different purposes:

| Dimension | `include_router(router)` | `mount("/prefix", sub_app)` |
|---|---|---|
| **Identity** | Routes become part of the parent app | Sub-app retains full independence |
| **OpenAPI** | Unified schema, single `/docs` | Separate schemas, separate `/docs` |
| **Dependencies** | Parent's `Depends()` chain propagates | No dependency propagation |
| **Middleware** | Single shared middleware stack | Double stack (parent wraps sub-app's own) |
| **Exception handlers** | Shared with parent | Independent per sub-app |
| **Lifespan** | Shared with parent | Independent (but sub-app's won't fire — see §6) |
| **Accepts** | Only `APIRouter` instances | Any ASGI app (FastAPI, Starlette, Flask via WSGI, Litestar) |

As Tiangolo stated: "`include_router` doesn't mount an app — it creates a path operation for each path operation in the included router. That's what allows it to be in the same OpenAPI, the same app, the same docs" [31].

**For your platform architecture**, the choice maps cleanly to your two app types. For apps that are "a set of Python functions auto-wrapped into FastAPI endpoints," use `include_router()` — create an `APIRouter` programmatically from the function registry, giving you unified docs and shared auth dependencies. For apps that are "a standalone FastAPI/ASGI app object," use `mount()` — this preserves their independence and lets them bring their own middleware and routing [1][28].

```python
from fastapi import FastAPI, APIRouter
from importlib import import_module

app = FastAPI()

for app_config in registry:
    if app_config["type"] == "functions":
        router = APIRouter(prefix=f"/api/{app_config['name']}")
        for func_name, func in app_config["functions"].items():
            router.add_api_route(f"/{func_name}", func, methods=["POST"])
        app.include_router(router)
    elif app_config["type"] == "asgi_app":
        module = import_module(app_config["module"])
        sub_app = getattr(module, app_config["app_attr"])
        app.mount(f"/api/{app_config['name']}", sub_app)
```

---

## 3. Dynamic mounting without server restart

Starlette's `app.routes` is a plain Python list — fully mutable at runtime [3]. You can append, remove, or replace `Mount` objects, and changes take effect on the next request because the `Router` iterates `self.routes` fresh on every dispatch.

```python
from starlette.routing import Mount

def register_app(parent: FastAPI, prefix: str, sub_app):
    parent.routes.append(Mount(prefix, app=sub_app))
    parent.openapi_schema = None         # Clear cached OpenAPI schema
    parent.middleware_stack = None        # Force middleware stack rebuild

def unregister_app(parent: FastAPI, prefix: str):
    parent.routes = [r for r in parent.routes
                     if not (isinstance(r, Mount) and r.path == prefix)]
    parent.openapi_schema = None
    parent.middleware_stack = None
```

**Two caches must be invalidated.** First, `app.openapi_schema` caches the OpenAPI spec after first generation — new routes won't appear in `/docs` unless you set it to `None` [14]. Second, `app.middleware_stack` is built lazily on the first request and cached thereafter; setting it to `None` forces a rebuild so new mounts are wrapped by middleware [14][24].

**Thread-safety is the main concern.** There is no locking mechanism in Starlette for route modification [14]. CPython's GIL makes `list.append()` atomic, so adding a mount during active request handling is reasonably safe. However, **removing or replacing routes while concurrent requests are iterating the routes list is unsafe**. For a config-driven platform where changes happen at startup or via an admin endpoint (not under heavy concurrent load), this is acceptable. For true hot-reload under load, wrap modifications in an asyncio lock:

```python
import asyncio

_route_lock = asyncio.Lock()

async def safe_register(parent, prefix, sub_app):
    async with _route_lock:
        register_app(parent, prefix, sub_app)
```

A more robust pattern for fully dynamic dispatch avoids mutating the routes list entirely by using a **custom ASGI dispatcher**:

```python
class DynamicDispatcher:
    def __init__(self):
        self.apps: dict[str, ASGIApp] = {}

    async def __call__(self, scope, receive, send):
        path = scope["path"]
        for prefix, app in self.apps.items():
            if path.startswith(prefix):
                scope["path"] = path[len(prefix):] or "/"
                scope["root_path"] = scope.get("root_path", "") + prefix
                await app(scope, receive, send)
                return
        response = JSONResponse({"detail": "App not found"}, status_code=404)
        await response(scope, receive, send)

dispatcher = DynamicDispatcher()
app = FastAPI()
app.mount("/apps", dispatcher)

# Register/unregister apps at runtime:
dispatcher.apps["/chord_analyzer"] = chord_analyzer_app
```

Litestar is worth noting as the one major framework with **explicit built-in `app.register()` support** for runtime route addition [15].

---

## 4. Middleware propagation — what flows down and what doesn't

**Starlette-style middleware (`app.add_middleware()`) does propagate to mounted sub-apps.** The architectural reason is clear: middleware wraps the Router, and the Router dispatches to Mounts, so all requests — regardless of which mount they hit — pass through the parent's middleware chain [2][38].

```python
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"])  # ✅ Applies to all mounts
app.add_middleware(CustomAuthMiddleware)                  # ✅ Applies to all mounts
app.mount("/api/v1", v1_app)
app.mount("/api/v2", v2_app)
# Both v1_app and v2_app receive CORS headers and auth checks
```

**FastAPI dependencies (`Depends()`) do not propagate.** Dependencies are resolved per-route within a specific app's dependency injection container. A mounted sub-app has its own container [1]. For shared auth, use ASGI middleware rather than FastAPI dependencies:

```python
class AuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            token = headers.get(b"authorization", b"").decode()
            if not verify_token(token):
                response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

app.add_middleware(AuthMiddleware)  # Wraps everything including mounts
```

**Edge cases where middleware fails to propagate correctly:**

- **Error responses from `ServerErrorMiddleware` bypass user middleware.** If an unhandled exception is caught by the outermost `ServerErrorMiddleware`, the 500 response it generates does not pass back through CORS middleware — so error responses may lack CORS headers [2][29].
- **`BaseHTTPMiddleware` breaks `ContextVar` propagation** because it runs the inner app in a copied task context. Context variables set by endpoints don't propagate back to the middleware's response processing [2].
- **Mount-level middleware** (via `Mount("/path", app=sub, middleware=[...])`) is **not wrapped in `ExceptionMiddleware`**, so exceptions in mount-level middleware bypass the parent's exception handling [13].
- If the sub-app also adds `CORSMiddleware`, you get **duplicate CORS headers** — configure CORS only on the parent [2].

---

## 5. Error isolation is strong by default

**An unhandled exception in a mounted sub-app crashes only the individual request, never the server process.** Three layers of protection ensure this.

First, if the sub-app is a full `FastAPI()` or `Starlette()` instance, it has its own `ServerErrorMiddleware` that catches all exceptions and returns a 500 response [2][29]. Second, even if something escapes the sub-app, the parent's `ServerErrorMiddleware` (outermost layer) catches it. Third, Uvicorn's protocol handlers wrap the ASGI app call in `try/except` — an escaping exception logs `"Exception in ASGI application"` and the server continues serving [17][21].

**One important caveat**: if response headers have already been sent when the exception occurs (e.g., during streaming), Uvicorn cannot send a proper 500 status code — the client receives an incomplete response [17].

For additional isolation, wrap untrusted sub-apps in error-catching middleware:

```python
class IsolationMiddleware:
    def __init__(self, app: ASGIApp, app_name: str):
        self.app = app
        self.app_name = app_name

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            logger.exception(f"Unhandled error in sub-app '{self.app_name}'")
            if scope["type"] == "http":
                resp = JSONResponse(
                    {"detail": f"Internal error in {self.app_name}"},
                    status_code=500
                )
                await resp(scope, receive, send)

app.mount("/risky", IsolationMiddleware(risky_app, "risky"))
```

---

## 6. Lifespan events don't fire for mounted sub-apps

This is the sharpest edge in ASGI composition. **Starlette explicitly does not propagate lifespan events to mounted sub-apps.** The FastAPI docs state: "Keep in mind that these lifespan events (startup and shutdown) will only be executed for the main application, not for Sub Applications - Mounts" [4]. The underlying Starlette issue (#649) has been open since September 2019 and is tagged for "Version 1.x" [10].

The ASGI server sends `lifespan.startup` and `lifespan.shutdown` only to the top-level app. The top-level Router handles these events but does not forward them to mounted sub-apps.

**The workaround is a `cascade_lifespan` pattern** from FastAPI discussion #9397 [11]:

```python
import contextlib
from contextlib import asynccontextmanager
from starlette.routing import Mount

@asynccontextmanager
async def cascade_lifespan(app: FastAPI):
    async with contextlib.AsyncExitStack() as stack:
        # Trigger lifespan for each mounted sub-app
        for route in app.routes:
            if isinstance(route, Mount) and hasattr(route.app, 'router'):
                ctx = route.app.router.lifespan_context
                await stack.enter_async_context(ctx(route.app))
        yield

app = FastAPI(lifespan=cascade_lifespan)
```

**`app.state` is also not shared.** Inside a sub-app handler, `request.app` refers to the sub-app, not the parent. Accessing `request.app.state.foo` will fail if `foo` was set on the parent [12]. The simplest workaround is to inject a reference before mounting:

```python
sub_app.state.db = app.state.db  # Share database pool
sub_app.state.config = app.state.config
app.mount("/api/v1", sub_app)
```

For your platform, initialize shared resources (database pools, caches, config) in the parent's lifespan, then distribute them to sub-apps via state injection before mounting. The `cascade_lifespan` pattern handles sub-apps that need their own startup/shutdown logic.

---

## 7. Performance at 50 mounts is not a bottleneck

Starlette uses a **linear scan with compiled regex matching** for route dispatch [3][33]. On each request, the Router iterates `self.routes` and calls `route.matches(scope)`, which runs a regex match against the request path. For `Mount` objects, this is a simple prefix regex like `^/api/v1(?P<path>.*)$`.

With 50 mounts, worst-case routing is **50 regex prefix matches per request** — roughly **10–50 microseconds** on modern hardware [23]. This is negligible compared to typical request processing (milliseconds for I/O, serialization, database queries). No GitHub issues, blog posts, or benchmarks report mount count as a performance bottleneck [23].

**Each `FastAPI()` instance does carry overhead**: its own middleware stack, OpenAPI schema generator, and exception handlers. For 50 sub-apps, this means 50 middleware stacks in memory — but these are built lazily and are small objects. The primary cost is memory, not per-request latency.

**Optimization tips** for large mount counts:

- **Order mounts by frequency** — place the most-accessed apps first in the routes list, since matching is sequential [3].
- **Use hierarchical mounts** to create a tree structure (e.g., mount `/api` with a Router that itself contains mounts for each app). This reduces the number of comparisons at each level.
- For extreme scale, Litestar's **radix-tree router** (written in Rust) provides O(log n) matching that is "agnostic to the number of routes" [15] — though switching frameworks is a large commitment.

Starlette discussion #2541 explored replacing regex-based prefix matching with simple `str.removeprefix()`, showing a modest **~1–2% RPS improvement** [23]. This confirms that routing overhead is already minimal.

---

## 8. Any ASGI-compliant app can be mounted

The ASGI specification defines an application as any callable with signature `async def app(scope, receive, send)` [16]. Starlette's `Mount` accepts any `ASGIApp`, so **any framework implementing ASGI v3 is mountable** — Starlette, Litestar, Quart, BlackSheep, Django Channels, and others.

```python
from fastapi import FastAPI
from litestar import Litestar, get as litestar_get

@litestar_get("/hello")
async def litestar_hello() -> dict:
    return {"from": "litestar"}

litestar_app = Litestar(route_handlers=[litestar_hello])
fastapi_app = FastAPI()
fastapi_app.mount("/litestar", litestar_app)  # Works — Litestar is ASGI
```

**WSGI apps (Flask, Django) require an adapter.** FastAPI officially recommends the `a2wsgi` package (their own `WSGIMiddleware` is deprecated) [5][19]:

```python
from a2wsgi import WSGIMiddleware
from flask import Flask

flask_app = Flask(__name__)

@flask_app.route("/")
def flask_index():
    return "Hello from Flask"

app.mount("/flask", WSGIMiddleware(flask_app))
```

**Compatibility boundaries to watch:**

- **WSGI apps run in a thread pool** (since WSGI is synchronous), consuming one thread per concurrent request. Under heavy load, this can exhaust the thread pool [5].
- **Flask's pseudo-global context** (`request`, `current_app`) can get mixed between concurrent requests when run via WSGIMiddleware. Tiangolo recommends deploying Flask separately behind a proxy for production use [34].
- **No WebSocket support** through WSGIMiddleware — WSGI has no WebSocket concept [5].
- Different frameworks may handle `root_path` differently, causing URL generation issues in mounted sub-apps.

---

## 9. In-process mounting beats reverse proxies for small platforms

For a single-developer platform with 10–50 lightweight apps, **in-process mounting is strongly preferred** over a reverse proxy architecture. The reasoning:

| Factor | In-Process Mounting | Reverse Proxy |
|---|---|---|
| **Operational complexity** | One process, one systemd unit, one log stream | N processes, N configs, orchestration |
| **Latency** | Zero network overhead | ~0.1–1ms per proxied request |
| **Memory** | Shared interpreter, shared connection pools | N × Python interpreter overhead |
| **Shared state** | Direct object references | Redis/database required |
| **Debugging** | Single stack trace | Distributed tracing needed |
| **Isolation** | One crash affects all apps | Full process isolation |

The consensus from engineering literature is clear: "Small teams benefit enormously from the simplicity and speed of a monolith" [6]. Companies like GitHub, Shopify, and Stack Overflow run successful monoliths at significant scale.

**The recommended hybrid approach:** Mount all lightweight apps in a single FastAPI process. Separate out only apps that have conflicting dependencies, need different Python versions, are CPU-intensive (blocking the event loop), or are experimental/untrusted. Use **Caddy** as a front-end reverse proxy — it provides automatic HTTPS via Let's Encrypt with zero configuration and native WebSocket proxying [6]:

```
# Caddyfile
platform.example.com {
    reverse_proxy /api/* localhost:8000    # Main ASGI process
    reverse_proxy /ml/* localhost:8001     # Heavy ML service (separate process)
}
```

---

## 10. WebSocket routing works across mounts with caveats

WebSocket connections route through mounts identically to HTTP requests. Starlette's `Mount` matches on `scope["path"]` regardless of whether `scope["type"]` is `"http"` or `"websocket"` [3][32]. A mounted sub-app with WebSocket routes works:

```python
from fastapi import FastAPI, WebSocket
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute

async def ws_handler(websocket: WebSocket):
    await websocket.accept()
    async for data in websocket.iter_text():
        await websocket.send_text(f"Echo: {data}")

ws_app = Starlette(routes=[WebSocketRoute("/stream", ws_handler)])
app = FastAPI()
app.mount("/ws", ws_app)
# Connect to: ws://localhost:8000/ws/stream
```

**Three WebSocket-specific gotchas:**

- **Trailing slash redirect breaks WebSocket clients.** Accessing `/ws` (no trailing slash) triggers a 307 redirect to `/ws/`. WebSocket clients typically do not follow HTTP redirects. Always use trailing slashes in WebSocket URLs, or ensure your mount paths match exactly [32].
- **Broad `Mount("/")` captures WebSocket requests** intended for more specific `WebSocketRoute` entries listed later. This causes an `AssertionError` because StaticFiles only handles HTTP scope. Place specific WebSocket routes *before* broad mounts in the routes list [32].
- **`BaseHTTPMiddleware` does not handle WebSocket scope** — it only processes `scope["type"] == "http"`. WebSocket connections pass through it unmodified, which is usually fine, but custom logic in `BaseHTTPMiddleware` won't apply to WebSockets. Use pure ASGI middleware for WebSocket-aware processing [2].

**CORS middleware does not interfere with WebSocket connections.** Starlette's `CORSMiddleware` checks for `scope["type"] == "http"` and passes WebSocket scopes through untouched. However, browsers do enforce same-origin policy on the initial WebSocket upgrade handshake — validate the `Origin` header manually in your WebSocket endpoint if needed [2].

---

## 11. No dedicated library exists — build on proven primitives

The Python ecosystem has **no production-grade library for dynamic multi-app ASGI composition** with features like hot-mounting, unified OpenAPI merging, and cross-app lifecycle management. The available tools are building blocks:

- **Starlette `Mount`** — the standard mechanism. Static composition, path-prefix delegation. Core of FastAPI [1][3].
- **Hypercorn `DispatcherMiddleware`** — a dict-based path→app dispatcher, simpler than Mount but with no middleware wrapping or lifecycle management [18].
- **`a2wsgi`** — bidirectional WSGI↔ASGI adapter. Essential for mounting Flask/Django apps. Actively maintained, officially recommended by FastAPI [19].
- **`asgi-tools`** — lightweight toolkit with `RouterMiddleware` for URL-based routing to different ASGI apps. Supports asyncio, trio, and curio. Actively maintained (v1.4.0, Nov 2025) [20].
- **Django Channels `ProtocolTypeRouter`/`URLRouter`** — protocol-based dispatch, useful in Django-centric architectures [42].
- **Mangum** — wraps a single ASGI app for AWS Lambda. Not a composition tool, but you can compose first and wrap the result [36].
- **`asgi-routing`** — experimental Rust-based radix-tree router. Marked "no maintenance intended" [15].

For your platform, the most practical approach is a **custom orchestration layer** built atop `Mount` and `importlib`:

```python
import importlib
from fastapi import FastAPI
from starlette.routing import Mount

def build_platform(config: dict) -> FastAPI:
    app = FastAPI(lifespan=cascade_lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"])
    app.add_middleware(AuthMiddleware)

    for name, app_config in config["apps"].items():
        module = importlib.import_module(app_config["module"])
        sub_app = getattr(module, app_config["app_attr"])
        sub_app.state.platform = app.state  # Share platform state
        app.mount(f"/api/{name}", sub_app)

    return app
```

---

## 12. Production patterns and lessons from real projects

Several open-source projects demonstrate multi-app FastAPI architectures at meaningful scale:

**Netflix Dispatch** uses a modular router architecture — not sub-app mounting — with domain-based modules (auth, incidents, plugins) included via `include_router()`. Its plugin system for Slack/Jira/PagerDuty integrations influenced the widely-cited `fastapi-best-practices` repository [35][25].

**`modular-monolith-fastapi`** by YoraiLevi chose `mount()` over routers specifically for service isolation. The project's key contribution is a custom OpenAPI merging script that walks all mounts and sub-apps to produce a unified specification — solving the most painful limitation of `mount()` [26]. The author noted: "The Purpose of sub applications aka Mounts is to create a complete new FastAPI application under the existing one that is invisible to its parent almost completely."

**`fast-subs`** by rifatrakib provides a CLI tool for creating/managing FastAPI sub-applications, each with its own config. The project documents several problems encountered at scale: code duplication across sub-apps, shared virtual environment security concerns, configuration management complexity, and dependency compatibility issues [40].

The **plugin-driven auto-registration pattern** is the closest match to your platform's config-driven discovery model [27]:

```python
import importlib, pkgutil

def auto_discover_apps(app: FastAPI, plugin_dir: str = "apps"):
    for finder, name, ispkg in pkgutil.iter_modules([plugin_dir]):
        try:
            mod = importlib.import_module(f"{plugin_dir}.{name}")
            if hasattr(mod, "app"):
                app.mount(f"/api/{name}", mod.app)
            elif hasattr(mod, "router"):
                app.include_router(mod.router, prefix=f"/api/{name}")
        except Exception as e:
            logger.error(f"Failed to load app '{name}': {e}")
```

**Recurring problems across these projects:** OpenAPI schema fragmentation (each mount gets its own `/docs`), lifespan event propagation requiring custom workarounds, state sharing requiring explicit wiring, and the lack of a standard unmounting mechanism [24][10][12].

---

## Conclusion

Building a personal app platform on FastAPI's `mount()` is architecturally sound. The ASGI composition model is well-specified, middleware propagation works correctly at the parent level, error isolation is strong by default, and performance is not a concern at 50 mounts. The three areas requiring custom engineering are: **lifespan cascading** (use the `cascade_lifespan` pattern from §6), **dynamic registration** (mutate `app.routes` with cache invalidation per §3, or build a `DynamicDispatcher`), and **unified documentation** (either accept separate `/docs` per sub-app, or build a custom OpenAPI merger like `modular-monolith-fastapi`). Avoid `BaseHTTPMiddleware` entirely — use pure ASGI middleware for auth and logging to prevent the exception-swallowing bug. For shared auth, implement it as ASGI middleware rather than FastAPI dependencies, since dependencies don't cross mount boundaries. Start monolithic, extract only CPU-intensive or untrusted apps to separate processes behind Caddy.

## REFERENCES

[1] FastAPI Documentation. "Sub Applications - Mounts." https://fastapi.tiangolo.com/advanced/sub-applications/

[2] Starlette Documentation. "Middleware." https://www.starlette.io/middleware/

[3] Starlette Documentation. "Routing." https://www.starlette.io/routing/

[4] FastAPI Documentation. "Lifespan Events." https://fastapi.tiangolo.com/advanced/events/

[5] FastAPI Documentation. "Including WSGI - Flask, Django, others." https://fastapi.tiangolo.com/advanced/wsgi/

[6] FastAPI Documentation. "Behind a Proxy." https://fastapi.tiangolo.com/advanced/behind-a-proxy/

[7] Starlette. "PR #2352: Fix root_path handling for mounted apps." https://github.com/encode/starlette/pull/2352

[8] FastAPI. "Issue #4531: BaseHTTPMiddleware swallows exceptions in mounted sub-apps." https://github.com/fastapi/fastapi/issues/4531

[9] FastAPI. "Issue #4194: Mounting sub-apps under APIRouter does not work." https://github.com/fastapi/fastapi/issues/4194

[10] Starlette. "Issue #649: Lifespan events in sub-applications." https://github.com/encode/starlette/issues/649

[11] FastAPI. "Discussion #9397: Cascade lifespan to mounted sub-apps." https://github.com/fastapi/fastapi/discussions/9397

[12] FastAPI. "Discussion #13908: Sub-app cannot access parent app state." https://github.com/fastapi/fastapi/discussions/13908

[13] Starlette. "PR #1649: Mount-level middleware not wrapped in ExceptionMiddleware." https://github.com/encode/starlette/pull/1649

[14] FastAPI. "Issue #1430: Adding service endpoints at runtime." https://github.com/fastapi/fastapi/issues/1430

[15] Litestar Documentation. "Routing Overview." https://docs.litestar.dev/2/usage/routing/overview.html

[16] ASGI Documentation. "ASGI Specification." https://asgi.readthedocs.io/en/latest/specs/main.html

[17] Uvicorn Documentation. "Server Behavior." https://www.uvicorn.org/server-behavior/

[18] Hypercorn Documentation. "Dispatching to multiple ASGI applications." https://hypercorn.readthedocs.io/en/stable/how_to_guides/dispatch_apps.html

[19] a2wsgi. "ASGI/WSGI bidirectional adapter." https://pypi.org/project/a2wsgi/

[20] asgi-tools. "ASGI toolkit for building apps and middleware." https://github.com/klen/asgi-tools

[21] Honeybadger. "Error Handling in FastAPI." https://www.honeybadger.io/blog/fastapi-error-handling/

[22] FastAPI. "Discussion #8849: OpenAPI schema merging for mounted sub-apps." https://github.com/fastapi/fastapi/discussions/8849

[23] Starlette. "Discussion #2541: Route matching performance optimization." https://github.com/encode/starlette/discussions/2541

[24] FastAPI. "Discussion #9995: No built-in way to unmount sub-applications." https://github.com/fastapi/fastapi/discussions/9995

[25] Zhanymkanov. "FastAPI Best Practices." GitHub. https://github.com/zhanymkanov/fastapi-best-practices

[26] YoraiLevi. "modular-monolith-fastapi." GitHub. https://github.com/YoraiLevi/modular-monolith-fastapi

[27] Rana B. "How I Built a Plugin-Driven FastAPI Backend That Auto-Registers Routes." Medium. https://medium.com/@bhagyarana80/how-i-built-a-plugin-driven-fastapi-backend-that-auto-registers-routes-e815a7298c29

[28] FastAPI Documentation. "Bigger Applications - Multiple Files." https://fastapi.tiangolo.com/tutorial/bigger-applications/

[29] Starlette Documentation. "Exceptions." https://www.starlette.io/exceptions/

[30] PyCon US 2023. "Inside your Web framework: Intro to the ASGI spec, middleware and apps." https://pycon-archive.python.org/2023/schedule/presentation/5/

[31] FastAPI. "Discussion #7652: include_router vs mount distinction." https://github.com/fastapi/fastapi/discussions/7652

[32] Starlette. "Issue #1548: Mount prefix captures WebSocket requests." https://github.com/encode/starlette/issues/1548

[33] Dev.to. "The Core of FastAPI: A Deep Dive into Starlette." https://dev.to/leapcell/the-core-of-fastapi-a-deep-dive-into-starlette-59hc

[34] FastAPI. "Discussion #6749: Flask concurrency issues under WSGIMiddleware." https://github.com/fastapi/fastapi/discussions/6749

[35] Netflix. "Dispatch: Crisis management orchestration framework." GitHub. https://github.com/Netflix/dispatch

[36] Mangum. "ASGI adapter for AWS Lambda." GitHub. https://github.com/Kludex/mangum

[37] Starlette. "Issue #2625: BaseHTTPMiddleware exception handling regression." https://github.com/encode/starlette/issues/2625

[38] FastAPI Documentation. "Advanced Middleware." https://fastapi.tiangolo.com/advanced/middleware/

[39] FastAPI. "Discussion #9070: Mount vs include_router." https://github.com/fastapi/fastapi/discussions/9070

[40] rifatrakib. "fast-subs: FastAPI sub-applications demonstration." GitHub. https://github.com/rifatrakib/fast-subs

[41] Pgorecki. "Lato: Modular monolith framework." GitHub. https://github.com/pgorecki/lato

[42] Django Channels Documentation. "Routing." https://channels.readthedocs.io/en/stable/topics/routing.html