"""ASGI app composition for enlace.

Builds a single FastAPI application by mounting discovered sub-apps and applying
cross-cutting middleware. Handles lifespan cascading to mounted sub-apps
(Starlette does not do this natively).
"""

import contextlib
import importlib
import inspect
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from enlace.base import AppConfig, PlatformConfig
from enlace.discover import discover_apps


def build_backend(config: PlatformConfig) -> FastAPI:
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
    import os

    os.environ["ENLACE_MANAGED"] = "1"

    sub_apps: list[tuple[str, object]] = []

    for app_config in config.apps:
        # Process and external modes are proxied, not imported
        if app_config.mode in ("process", "external"):
            proxy_app = _make_proxy_for(app_config)
            if proxy_app is not None:
                sub_apps.append((app_config.route_prefix, proxy_app))
            continue

        # Static mode is handled separately below (with frontend files)
        if app_config.mode == "static":
            continue

        # asgi mode — original behavior
        if app_config.app_type == "frontend_only":
            continue
        sub_app = _load_sub_app(app_config)
        if sub_app is not None:
            sub_apps.append((app_config.route_prefix, sub_app))

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

    for prefix, sub_app in sub_apps:
        parent.mount(prefix, sub_app)

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
    for app_config in config.apps:
        if (
            app_config.mode != "static"
            and app_config.frontend_dir
            and app_config.frontend_dir.is_dir()
        ):
            frontend_prefix = f"/{app_config.name}"
            parent.mount(
                frontend_prefix,
                StaticFiles(directory=str(app_config.frontend_dir), html=True),
            )

    # Serve platform-level shared assets (e.g. shared.css) at the root.
    # Mounted last so it never shadows API or app-specific routes.
    if config.shared_assets_dir and config.shared_assets_dir.is_dir():
        parent.mount(
            "/",
            StaticFiles(directory=str(config.shared_assets_dir)),
        )

    return parent


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
    """
    config = discover_apps()
    config = _apply_port_env(config)
    return build_backend(config)


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
