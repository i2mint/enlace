"""ASGI app composition for enlace.

Builds a single FastAPI application by mounting discovered sub-apps and applying
cross-cutting middleware. Handles lifespan cascading to mounted sub-apps
(Starlette does not do this natively).

Plugins:
    ``build_backend`` accepts a ``plugins`` argument — a sequence of callables
    ``(parent: FastAPI, config: PlatformConfig) -> None`` invoked once after
    sub-apps are mounted. ``enlace_auth.plugin`` is the canonical example:
    when installed, it adds auth, sessions, the admin dashboard, and per-user
    stores. enlace itself is auth-agnostic.
"""

import contextlib
import importlib
import inspect
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional, Sequence

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from enlace.base import AppConfig, PlatformConfig
from enlace.discover import discover_apps
from enlace.frontend import SPAStaticFiles

_logger = logging.getLogger("enlace")

Plugin = Callable[[FastAPI, PlatformConfig], None]


class EnlaceConfigError(RuntimeError):
    """Raised when platform configuration is unusable at startup.

    Distinct from ``ValueError`` / ``RuntimeError`` so callers and tests can
    target this specific class.
    """


def build_backend(config: PlatformConfig, *, plugins: Sequence[Plugin] = ()) -> FastAPI:
    """Compose all app backends into a single ASGI application.

    For each discovered app:
    - mode=asgi, asgi_app: mount the ASGI object at the route prefix
    - mode=asgi, functions: build an APIRouter with POST routes and include it
    - mode=process/external: mount a reverse proxy at the route prefix
    - mode=static: mount StaticFiles at the route prefix
    - frontend_only (mode=asgi): skip (no backend to mount)

    Args:
        config: Platform configuration with apps already discovered.

    Returns:
        A FastAPI application with all sub-apps mounted.
    """
    # Signal to sub-apps that they're running under enlace, so they can skip
    # their own CORS middleware, standalone startup blocks, etc.
    os.environ["ENLACE_MANAGED"] = "1"

    sub_apps: list[tuple[str, str, object]] = []  # (route_prefix, app_id, asgi_app)

    for app_config in config.apps:
        # Process and external modes are proxied, not imported
        if app_config.mode in ("process", "external"):
            proxy_app = _make_proxy_for(app_config)
            if proxy_app is not None:
                sub_apps.append((app_config.route_prefix, app_config.name, proxy_app))
            continue

        # Static mode is handled separately below (with frontend files)
        if app_config.mode == "static":
            continue

        # asgi mode — original behavior
        if app_config.app_type == "frontend_only":
            continue
        sub_app = _load_sub_app(app_config)
        if sub_app is not None:
            sub_apps.append((app_config.route_prefix, app_config.name, sub_app))

    @asynccontextmanager
    async def cascade_lifespan(app: FastAPI):
        """Forward startup/shutdown to mounted sub-apps.

        Starlette does NOT propagate lifespan events to mounted sub-apps
        (issue #649, open since Sept 2019). This workaround iterates Mount
        routes and enters their lifespan contexts.
        """
        async with contextlib.AsyncExitStack() as stack:
            for route in app.routes:
                if isinstance(route, Mount) and hasattr(route.app, "router"):
                    lifespan = getattr(route.app.router, "lifespan_context", None)
                    if lifespan is not None:
                        await stack.enter_async_context(lifespan(route.app))
            yield

    parent = FastAPI(
        title="enlace platform",
        lifespan=cascade_lifespan,
    )

    # CORS on the parent only — sub-apps must NOT add their own CORS
    parent.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Dev default; restrict in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Compose-time plugins (pure ASGI middleware only — no BaseHTTPMiddleware
    # in plugins either, please). enlace_auth is the canonical plugin: it
    # mounts /auth/*, /_admin/*, store routes, and the auth+csrf middleware.
    for plug in plugins:
        plug(parent, config)

    # JSON listing is always on (cheap, useful for frontends even when the
    # HTML index_page is disabled).
    _add_apps_listing_route(parent, config)

    # landing_app takes precedence over the default Python index. When set
    # to a discovered app's name, the later frontend-mount loop will mount
    # that app's frontend at / (see below). The built-in index is skipped.
    landing_app_name = config.landing_app
    if landing_app_name and not any(a.name == landing_app_name for a in config.apps):
        raise ValueError(
            f"platform.landing_app = {landing_app_name!r} but no such app "
            f"was discovered. Known apps: "
            f"{sorted(a.name for a in config.apps)}"
        )

    if config.index_page and not landing_app_name:
        _add_index_route(parent, config)

    for prefix, app_id, sub_app in sub_apps:
        parent.mount(prefix, _AppIdInjector(sub_app, app_id))

    # Serve static-mode apps at their route prefix.
    for app_config in config.apps:
        if app_config.mode == "static":
            static_dir = app_config.public_dir or app_config.frontend_dir
            if static_dir and static_dir.is_dir():
                parent.mount(
                    app_config.route_prefix,
                    StaticFiles(directory=str(static_dir), html=True),
                )

    # Serve frontend static files for apps that have a frontend/ directory.
    # Mounted at /{app_name}/ so the frontend is accessible alongside the API.
    # Uses SPAStaticFiles so client-side routing (e.g. /projects/{id}) falls
    # back to index.html instead of returning 404.
    # The landing_app (if any) is mounted LAST at / so it catches the root.
    landing_app_config: Optional[AppConfig] = None
    for app_config in config.apps:
        if (
            app_config.mode != "static"
            and app_config.frontend_dir
            and app_config.frontend_dir.is_dir()
        ):
            if app_config.name == landing_app_name:
                landing_app_config = app_config
                # Still also mount at /{name}/ so the app is reachable by
                # name; some deployments may link to both.
            frontend_prefix = f"/{app_config.name}"

            # Starlette mounts only match paths with trailing slash.
            # Add a redirect so /{app_name} → /{app_name}/ works.
            @parent.get(frontend_prefix, include_in_schema=False)
            async def _redirect(prefix=frontend_prefix):
                return RedirectResponse(f"{prefix}/")

            parent.mount(
                frontend_prefix,
                SPAStaticFiles(directory=str(app_config.frontend_dir), html=True),
            )

    # landing_app: mount the chosen app's frontend at / as well, so the
    # platform's root URL serves it instead of the default Python index.
    # Mounted BEFORE shared_assets_dir so the landing's index.html wins at /.
    if landing_app_config is not None:
        parent.mount(
            "/",
            SPAStaticFiles(directory=str(landing_app_config.frontend_dir), html=True),
        )

    # Serve platform-level shared assets (e.g. shared.css) at the root.
    # Mounted last so it never shadows API or app-specific routes.
    if config.shared_assets_dir and config.shared_assets_dir.is_dir():
        parent.mount(
            "/",
            StaticFiles(directory=str(config.shared_assets_dir)),
        )

    return parent


def _can_access(
    access: str,
    user_id: Optional[str],
    user_email: Optional[str],
    allowed_users: list[str],
) -> bool:
    """Whether a request can see an app of this access level in /_apps.

    `public` / `local` → always.
    `protected:user`   → only if authenticated AND (no allowed_users list, or
                         the user's email is in it).
    `protected:shared` → visible either way (gated at open-time, not
                         discovery-time — users should know the app exists
                         so they can ask for the password).
    """
    if access in ("public", "local", "protected:shared"):
        return True
    if access == "protected:user":
        if user_id is None:
            return False
        if allowed_users:
            who = user_email or user_id
            return who in allowed_users
        return True
    return False


def _add_apps_listing_route(parent: FastAPI, config: PlatformConfig) -> None:
    """Add GET /_apps returning a JSON list filtered by the caller's access.

    Used by frontend landing pages (e.g. ``apps/landing/``) to render the
    app grid. The response is deliberately minimal — just what the UI needs.
    The landing app itself is hidden — it's the shell, not a listable app.
    """
    apps = config.apps
    landing_name = config.landing_app

    @parent.get("/_apps")
    async def apps_listing(request: Request) -> dict:
        user_id = getattr(request.state, "user_id", None)
        user_email = getattr(request.state, "user_email", None)
        items = []
        for app in apps:
            if app.name == landing_name:
                continue
            if not _can_access(app.access, user_id, user_email, app.allowed_users):
                continue
            items.append(
                {
                    "name": app.name,
                    "display_name": app.display_name,
                    "route": f"/{app.name}/",
                    "api_route": app.route_prefix,
                    "access": app.access,
                    "has_frontend": bool(
                        app.frontend_dir and app.frontend_dir.is_dir()
                    ),
                    "has_api": app.app_type != "frontend_only",
                }
            )
        return {
            "apps": items,
            "user": {"id": user_id, "email": user_email} if user_id else None,
        }


def _add_index_route(parent: FastAPI, config: PlatformConfig) -> None:
    """Add a GET / route that lists all discovered apps as a simple HTML page."""
    apps = config.apps

    @parent.get("/", response_class=HTMLResponse)
    async def index():
        items = []
        for app in apps:
            has_frontend = app.frontend_dir and app.frontend_dir.is_dir()
            has_api = app.app_type != "frontend_only"
            links = []
            if has_frontend:
                links.append(f'<a href="/{app.name}/">open</a>')
            if has_api:
                links.append(f'<a href="{app.route_prefix}/docs">api docs</a>')
            link_html = " · ".join(links) if links else "(no routes)"
            items.append(f"<li><strong>{app.display_name}</strong> — {link_html}</li>")
        app_list = "\n".join(items) if items else "<li>No apps discovered.</li>"
        return (
            "<!doctype html>"
            "<html><head><meta charset='utf-8'>"
            "<title>enlace</title>"
            "<style>"
            "body{font-family:system-ui,sans-serif;max-width:640px;"
            "margin:40px auto;padding:0 20px;color:#333}"
            "a{color:#2563eb} li{margin:8px 0}"
            "</style></head><body>"
            "<h1>enlace</h1>"
            f"<p>{len(apps)} app{'s' if len(apps) != 1 else ''} running:</p>"
            f"<ul>{app_list}</ul>"
            "</body></html>"
        )


class _AppIdInjector:
    """Pure-ASGI wrapper that stamps the app's name into scope state.

    Sub-apps never read this themselves — ``StoreInjectionMiddleware`` does,
    so it knows which app's per-user store to attach.
    """

    def __init__(self, app, app_id: str):
        self.app = app
        self.app_id = app_id

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            state = scope.setdefault("state", {})
            state["app_id"] = self.app_id
        await self.app(scope, receive, send)


def _make_proxy_for(app_config: AppConfig) -> Optional[object]:
    """Create a reverse proxy ASGI app for a process or external backend.

    Returns None if the upstream cannot be determined (no port/upstream_url).
    """
    from enlace.proxy import make_proxy_app

    if app_config.mode == "external" and app_config.upstream_url:
        return make_proxy_app(
            upstream=app_config.upstream_url,
            strip_prefix=app_config.route_prefix,
        )
    elif app_config.mode == "process" and app_config.port is not None:
        upstream = f"http://127.0.0.1:{app_config.port}"
        return make_proxy_app(
            upstream=upstream,
            strip_prefix=app_config.route_prefix,
        )
    return None


def _load_sub_app(app_config: AppConfig) -> Optional[object]:
    """Import and return the sub-app for a given AppConfig.

    For asgi_app type: imports the module and returns the app attribute.
    For functions type: builds an APIRouter wrapping the module's public functions.

    Uses app_config.source_dir for correct sys.path resolution.
    """
    if app_config.entry_module_path is None:
        return None

    source_dir = app_config.source_dir
    if source_dir is None:
        # Fallback for manually-constructed AppConfigs without source_dir
        source_dir = app_config.entry_module_path.parent.parent

    module = _import_app_module(app_config.entry_module_path, source_dir)

    if app_config.app_type == "asgi_app":
        sub_app = getattr(module, app_config.app_attr, None)
        if sub_app is None:
            raise AttributeError(
                f"Module {app_config.entry_module_path} has no attribute "
                f"'{app_config.app_attr}'"
            )
        return sub_app

    elif app_config.app_type == "functions":
        return _build_router_from_functions(module, app_config)

    return None


def _build_router_from_functions(module, app_config: AppConfig) -> FastAPI:
    """Build a FastAPI sub-app from a module's public typed functions."""
    sub_app = FastAPI(title=app_config.display_name)

    for name, func in inspect.getmembers(module, inspect.isfunction):
        if name.startswith("_") or func.__module__ != module.__name__:
            continue
        # Use GET for no-param functions, POST for functions with params
        sig = inspect.signature(func)
        params = [p for p in sig.parameters.values() if p.name not in ("self", "cls")]
        method = "GET" if not params else "POST"
        sub_app.add_api_route(f"/{name}", func, methods=[method])

    return sub_app


def _import_app_module(entry_path: Path, source_dir: Path):
    """Import the app module from a filesystem path.

    Uses source_dir.parent as the sys.path root. The path is added
    permanently (needed for the lifetime of the server process).
    """
    root = str(source_dir.parent.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    relative = entry_path.resolve().relative_to(Path(root).resolve())
    module_name = str(relative.with_suffix("")).replace("/", ".").replace("\\", ".")
    if module_name in sys.modules:
        del sys.modules[module_name]
    return importlib.import_module(module_name)


def create_app() -> FastAPI:
    """App factory for Uvicorn's --factory flag.

    Loads platform config, discovers apps, checks conflicts,
    and builds the composed backend.

    Plugins are loaded from the ``ENLACE_PLUGINS`` env var: a comma-separated
    list of ``module:attribute`` pairs, e.g.::

        ENLACE_PLUGINS=enlace_auth:plugin

    Each resolved object must be a callable
    ``(parent: FastAPI, config: PlatformConfig) -> None``.
    """
    config = discover_apps()
    config = _apply_port_env(config)
    plugins = _load_plugins_from_env()
    return build_backend(config, plugins=plugins)


def _load_plugins_from_env() -> list[Plugin]:
    """Parse ENLACE_PLUGINS=mod:attr,mod2:attr2 and resolve to callables."""
    raw = os.environ.get("ENLACE_PLUGINS", "").strip()
    if not raw:
        return []
    out: list[Plugin] = []
    for spec in (s.strip() for s in raw.split(",")):
        if not spec:
            continue
        if ":" not in spec:
            raise EnlaceConfigError(
                f"ENLACE_PLUGINS entry {spec!r} is not in 'module:attribute' form"
            )
        mod_name, attr = spec.split(":", 1)
        try:
            mod = importlib.import_module(mod_name)
        except ImportError as e:
            raise EnlaceConfigError(
                f"ENLACE_PLUGINS: cannot import {mod_name!r}: {e}"
            ) from e
        try:
            obj = getattr(mod, attr)
        except AttributeError as e:
            raise EnlaceConfigError(
                f"ENLACE_PLUGINS: {mod_name!r} has no attribute {attr!r}"
            ) from e
        if not callable(obj):
            raise EnlaceConfigError(
                f"ENLACE_PLUGINS: {spec!r} resolved to non-callable {obj!r}"
            )
        out.append(obj)
    return out


def _apply_port_env(config: PlatformConfig) -> PlatformConfig:
    """Apply auto-allocated ports from the ENLACE_PROCESS_PORTS env var.

    When serve.py runs in mixed mode, it auto-allocates ports for process
    apps and stores them in an env var so the gateway subprocess can read
    them and set up correct proxy routes.
    """
    import json
    import os

    raw = os.environ.get("ENLACE_PROCESS_PORTS", "")
    if not raw:
        return config
    try:
        port_map = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return config

    updated_apps = []
    for app in config.apps:
        if app.mode == "process" and app.name in port_map:
            port = int(port_map[app.name])
            app = app.model_copy(update={"port": port})
        updated_apps.append(app)

    return config.model_copy(update={"apps": updated_apps})
