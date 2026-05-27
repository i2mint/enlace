"""Backend-strategy registry and built-in strategies for enlace.

A ``BackendStrategy`` owns everything mode-specific about an app:

- which ``app.toml`` keys it understands (``toml_field_map`` / ``path_keys``);
- whether discovery should skip Python introspection
  (``skip_python_introspection``);
- mode-specific config validation (``validate``);
- how to expose the app over HTTP (``make_asgi`` — returns an ASGI sub-app
  to mount, ``None`` for no HTTP);
- how to run the app as a managed process (``make_lifecycle`` — returns a
  ``Lifecycle`` to supervise, ``None`` for no process).

This module is the **open/closed extension point** for new backend modes.
The four built-in strategies (``asgi``, ``process``, ``external``,
``static``) capture today's behavior verbatim. External plugins like
``enlace_docker`` register additional strategies via the
``enlace.backend_strategies`` entry-point group; installing the plugin
package is enough — no user code change required.

Registry semantics:

- ``register_strategy`` is idempotent on ``name`` collisions: the latest
  registration wins (so plugins can override built-ins if they really must).
- ``get_strategy`` performs a *lazy* entry-point scan on cache miss, so
  tests and direct callers don't need to import plugin packages manually.
- The four built-ins are registered eagerly at module import.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from enlace.base import AppConfig, PlatformConfig

_logger = logging.getLogger("enlace.strategies")

# ASGI is a callable conforming to the three-arg (scope, receive, send) shape;
# we keep the alias loose since concrete sub-apps may be FastAPI, StaticFiles,
# or any other ASGI-compatible object.
ASGIApp = Callable[..., Any]


# -- Lifecycle protocol -------------------------------------------------------


@runtime_checkable
class Lifecycle(Protocol):
    """Protocol for an enlace-supervised backend.

    The dev-mode supervisor (``enlace.supervise.supervise_all``) drives any
    object satisfying this protocol. ``ManagedProcess`` in ``supervise.py``
    is the canonical implementation for subprocess-backed apps; plugins
    (e.g. ``enlace_docker``) may provide other implementations (e.g. a
    container-backed lifecycle that shells out to ``docker``).

    The supervisor calls (in order, per attempt):
    ``start()`` → ``stream_logs()`` (concurrently with) ``wait_healthy()``
    → ``wait_exit()`` → ``should_restart()`` → ``backoff_delay()``.

    Restart accounting (``record_failure``, ``maybe_reset_backoff``) is
    handled by the lifecycle so each implementation can choose its own
    policy semantics.
    """

    name: str
    color: str  # writable; set by supervisor for log labeling
    state: str

    async def start(self) -> None: ...
    async def stop(self, timeout: float = 10.0) -> None: ...
    async def wait_healthy(self) -> bool: ...
    async def stream_logs(self) -> None: ...
    async def wait_exit(self) -> Optional[int]: ...

    def should_restart(self) -> bool: ...
    def record_failure(self) -> None: ...
    def maybe_reset_backoff(self) -> None: ...
    def backoff_delay(self) -> float: ...
    def log(self, msg: str) -> None: ...


# -- BackendStrategy ----------------------------------------------------------


class BackendStrategy:
    """Base class for backend strategies.

    Subclasses set ``name`` and override the methods relevant to their mode.
    Concrete subclasses for the built-in modes live below; external plugins
    subclass this and register their instance via ``register_strategy`` or
    the ``enlace.backend_strategies`` entry-point group.
    """

    name: str = ""

    # TOML keys this strategy reads from app.toml, mapped to AppConfig field
    # names. Used by ``enlace.discover`` to overlay user config onto the
    # discovered AppConfig. Cross-cutting keys (e.g. ``route``, ``access``,
    # ``display_name``) live in the core map in ``discover.py``.
    toml_field_map: dict[str, str] = {}

    # Subset of ``toml_field_map`` keys whose values should be resolved as
    # filesystem paths relative to the app directory.
    path_keys: set[str] = set()

    # When True, the discoverer skips Python module introspection for apps
    # in this mode (the app may not even be Python). The asgi strategy is
    # the only built-in that needs introspection.
    skip_python_introspection: bool = True

    # When True, ``enlace serve`` treats apps in this mode as needing a
    # supervised lifecycle (gateway runs as one child alongside them). When
    # False, the app is mounted or proxied but enlace does not manage any
    # process for it (e.g. asgi, external, static).
    is_supervisable: bool = False

    def validate(self, app: "AppConfig") -> None:
        """Validate mode-specific fields. Raise ``ValueError`` if invalid.

        Called from ``AppConfig`` Pydantic validators, so raising
        ``ValueError`` will be wrapped in a ``ValidationError`` automatically.
        """
        return None

    def make_asgi(
        self, app: "AppConfig", platform: "PlatformConfig"
    ) -> Optional[ASGIApp]:
        """Build the ASGI sub-app to mount at ``app.route_prefix``.

        Return ``None`` if this strategy contributes no HTTP routing (e.g.
        an asgi-mode ``frontend_only`` app).
        """
        return None

    def make_lifecycle(
        self, app: "AppConfig", platform: "PlatformConfig"
    ) -> Optional[Lifecycle]:
        """Build the supervised lifecycle for this app, or ``None``.

        Returning ``None`` means enlace does not manage a process for this
        app (e.g. ``asgi``, ``external``, ``static``).
        """
        return None


# -- Registry -----------------------------------------------------------------


_STRATEGIES: dict[str, BackendStrategy] = {}
_ENTRY_POINTS_LOADED = False
_ENTRY_POINT_GROUP = "enlace.backend_strategies"


def register_strategy(strategy: BackendStrategy) -> None:
    """Register a backend strategy under its ``name``.

    Idempotent on collisions: a later registration replaces an earlier one.
    """
    if not strategy.name:
        raise ValueError(
            f"BackendStrategy {type(strategy).__name__!r} has empty 'name' "
            "— strategies must declare a unique mode identifier."
        )
    _STRATEGIES[strategy.name] = strategy


def get_strategy(name: str) -> BackendStrategy:
    """Return the strategy registered for ``name``.

    On cache miss, performs a lazy scan of the ``enlace.backend_strategies``
    entry-point group so plugins are discovered without an explicit import.
    Raises ``ValueError`` with an actionable message if the mode is unknown
    after the scan.
    """
    if name in _STRATEGIES:
        return _STRATEGIES[name]
    _load_entry_points()
    if name in _STRATEGIES:
        return _STRATEGIES[name]
    known = ", ".join(sorted(_STRATEGIES)) or "(none)"
    raise ValueError(
        f"Unknown app mode {name!r}. Known modes: {known}. "
        f"If you expected a plugin to provide this mode, ensure it is "
        f"installed (e.g. `pip install enlace_docker` for docker modes)."
    )


def known_modes() -> list[str]:
    """Return a sorted list of registered mode names (after entry-point scan)."""
    _load_entry_points()
    return sorted(_STRATEGIES)


def iter_strategies() -> list[BackendStrategy]:
    """Return all registered strategies (after entry-point scan), sorted by name."""
    _load_entry_points()
    return [_STRATEGIES[name] for name in sorted(_STRATEGIES)]


def _load_entry_points() -> None:
    """Lazily discover and register strategies advertised via entry points.

    Each entry point in the ``enlace.backend_strategies`` group must resolve
    to a ``BackendStrategy`` subclass (not an instance) — we instantiate it
    here. Broken entry points are logged and skipped rather than crashing
    the platform.
    """
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True

    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover — Python <3.10 not supported anyway
        return

    try:
        # Python 3.10+: entry_points(group=...) returns the right type.
        eps = entry_points(group=_ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover — defensive
        eps = entry_points().get(_ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]

    for ep in eps:
        try:
            cls = ep.load()
        except Exception as e:  # pragma: no cover — plugin authors' problem
            _logger.warning(
                "Failed to load enlace backend strategy %r from %r: %s",
                ep.name,
                ep.value,
                e,
            )
            continue
        try:
            instance = cls() if isinstance(cls, type) else cls
        except Exception as e:  # pragma: no cover
            _logger.warning(
                "Failed to instantiate enlace backend strategy %r: %s", ep.name, e
            )
            continue
        if not isinstance(instance, BackendStrategy):
            _logger.warning(
                "Entry point %r resolved to %r which is not a BackendStrategy; "
                "skipping.",
                ep.name,
                instance,
            )
            continue
        register_strategy(instance)


# -- Built-in strategies ------------------------------------------------------


class AsgiStrategy(BackendStrategy):
    """Default mode: import the app's Python module and mount its ASGI callable.

    Also covers the ``functions`` app_type (auto-generated FastAPI routes
    wrapping the module's typed public functions) and ``frontend_only``
    (no backend to mount — returns ``None`` from ``make_asgi``).
    """

    name = "asgi"
    skip_python_introspection = False
    toml_field_map = {
        "entry_point": "entry_module_path",
        "app_attr": "app_attr",
    }
    path_keys = {"entry_point"}

    def make_asgi(self, app, platform):
        # frontend_only apps have no backend
        if app.app_type == "frontend_only":
            return None
        # Lazy import to avoid pulling FastAPI into modules that don't need it.
        from enlace.compose import _load_sub_app

        return _load_sub_app(app)


class ProcessStrategy(BackendStrategy):
    """Spawn the app as a child process, health-check, restart, proxy."""

    name = "process"
    is_supervisable = True
    toml_field_map = {
        "command": "command",
        "port": "port",
        "socket": "socket",
        "env": "env",
        "build": "build",
        "health_check_path": "health_check_path",
        "ready_timeout": "ready_timeout",
        "restart_policy": "restart_policy",
        "max_retries": "max_retries",
        "restart_delay_ms": "restart_delay_ms",
    }

    def validate(self, app):
        if not app.command:
            raise ValueError(f"App '{app.name}': mode='process' requires 'command'")
        if app.port is not None and app.socket is not None:
            raise ValueError(f"App '{app.name}': set 'port' or 'socket', not both")
        if app.port is None and app.socket is None:
            raise ValueError(
                f"App '{app.name}': mode='process' requires 'port' or 'socket'"
            )

    def make_asgi(self, app, platform):
        if app.port is None:
            return None
        from enlace.proxy import make_proxy_app

        upstream = f"http://127.0.0.1:{app.port}"
        return make_proxy_app(upstream=upstream, strip_prefix=app.route_prefix)

    def make_lifecycle(self, app, platform):
        from pathlib import Path

        from enlace.supervise import ManagedProcess

        cwd = (
            app.entry_module_path.parent
            if app.entry_module_path
            else (app.source_dir / app.name if app.source_dir else Path("."))
        )
        return ManagedProcess(
            name=app.name,
            command=app.command or [],
            cwd=cwd,
            port=app.port,
            socket_path=app.socket,
            env=app.env,
            health_check_path=app.health_check_path,
            ready_timeout=app.ready_timeout,
            restart_policy=app.restart_policy,
            max_retries=app.max_retries,
            restart_delay_ms=app.restart_delay_ms,
        )


class ExternalStrategy(BackendStrategy):
    """Route to a pre-existing service at a known URL; no lifecycle."""

    name = "external"
    toml_field_map = {
        "upstream_url": "upstream_url",
    }

    def validate(self, app):
        if not app.upstream_url:
            raise ValueError(
                f"App '{app.name}': mode='external' requires 'upstream_url'"
            )

    def make_asgi(self, app, platform):
        if not app.upstream_url:
            return None
        from enlace.proxy import make_proxy_app

        return make_proxy_app(upstream=app.upstream_url, strip_prefix=app.route_prefix)


class StaticStrategy(BackendStrategy):
    """Serve a directory of static files (no Python, no proxy)."""

    name = "static"
    toml_field_map = {
        "public_dir": "public_dir",
    }
    path_keys = {"public_dir"}

    def validate(self, app):
        if app.public_dir is None and app.frontend_dir is None:
            raise ValueError(
                f"App '{app.name}': mode='static' requires "
                "'public_dir' or 'frontend_dir'"
            )

    def make_asgi(self, app, platform):
        # Prefer public_dir; fall back to frontend_dir. Skip if neither
        # exists on disk — same conservatism as the legacy static loop.
        from starlette.staticfiles import StaticFiles

        static_dir = app.public_dir or app.frontend_dir
        if not static_dir or not static_dir.is_dir():
            return None
        return StaticFiles(directory=str(static_dir), html=True)


# Register the built-ins at import time so AppConfig validation works
# regardless of whether entry points have been scanned yet.
for _strategy in (
    AsgiStrategy(),
    ProcessStrategy(),
    ExternalStrategy(),
    StaticStrategy(),
):
    register_strategy(_strategy)


# -- Helpers for discover.py --------------------------------------------------


def collect_strategy_field_maps() -> tuple[dict[str, str], set[str]]:
    """Aggregate ``toml_field_map`` and ``path_keys`` across all strategies.

    Used by ``discover._overlay_toml_fields`` to know which TOML keys can
    map to which AppConfig fields. The core TOML keys (``route``,
    ``access``, ``display_name``, ``frontend_dir``, ``mode``, etc.) live in
    discover.py's own map; this only contributes mode-specific keys.

    Collisions between strategies on the same TOML key are resolved
    last-write-wins (in registration order). In practice the built-ins
    don't collide, and plugins are expected to use distinct keys.
    """
    field_map: dict[str, str] = {}
    path_keys: set[str] = set()
    for strategy in iter_strategies():
        field_map.update(strategy.toml_field_map)
        path_keys.update(strategy.path_keys)
    return field_map, path_keys
