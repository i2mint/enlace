"""Convention-based app discovery for enlace.

Walks an apps directory, discovers backend entry points and frontend assets,
detects app types, loads per-app TOML overrides, and returns validated AppConfig
objects with provenance tracking.
"""

import importlib
import inspect
import shlex
import sys
from pathlib import Path
from typing import Optional, Protocol

from enlace.base import AppConfig, ConventionsConfig, PlatformConfig
from enlace.util import derive_route_prefix, is_skippable

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]


class AppDiscoverer(Protocol):
    """Protocol for app discovery strategies."""

    def discover(self, apps_dir: Path) -> list[AppConfig]: ...


class ConventionDiscoverer:
    """Discovers apps by filesystem conventions.

    Walks the apps directory, finds entry points, detects app types,
    loads TOML overrides, and returns validated AppConfig objects.

    Args:
        conventions: Meta-conventions controlling discovery behavior.
    """

    def __init__(self, conventions: Optional[ConventionsConfig] = None):
        self.conventions = conventions or ConventionsConfig()

    def discover(self, apps_dir: Path) -> list[AppConfig]:
        """Discover all apps in the given directory.

        Args:
            apps_dir: Path to the directory containing app subdirectories.

        Returns:
            List of AppConfig objects, sorted by name.
        """
        if not apps_dir.exists():
            return []

        apps: list[AppConfig] = []
        for app_dir in sorted(apps_dir.iterdir()):
            if not app_dir.is_dir() or is_skippable(app_dir.name):
                continue
            config = self._discover_app(app_dir, apps_dir)
            if config is not None:
                apps.append(config)
        return apps

    def _discover_app(self, app_dir: Path, apps_dir: Path) -> Optional[AppConfig]:
        """Discover a single app from its directory."""
        from enlace.strategies import get_strategy

        name = app_dir.name
        route_prefix = derive_route_prefix(name)

        # Check app.toml first. The declared mode's strategy decides whether
        # to skip Python introspection (built-in: asgi inspects, all others
        # don't; plugins like enlace_docker also opt out).
        override_file = app_dir / "app.toml"
        toml_data = _load_toml(override_file) if override_file.exists() else {}

        declared_mode = toml_data.get("mode", "asgi")
        strategy = get_strategy(declared_mode)

        if strategy.skip_python_introspection:
            config = self._build_non_asgi_config(
                name,
                route_prefix,
                apps_dir,
                app_dir,
                toml_data,
                declared_mode,
            )
            return self._finalize_metadata(config, app_dir)

        # -- Standard asgi-mode discovery (current behavior) --
        entry_path = self._find_entry_point(app_dir)
        # app.toml's `frontend_dir` (if set) overrides the convention.
        # Without this, an app whose build outputs to e.g. ./dist would be
        # silently skipped — discovery would conclude "no frontend" and the
        # later _apply_overrides step never runs.
        frontend_override = toml_data.get("frontend_dir") if toml_data else None
        frontend_dir = self._find_frontend_dir(app_dir, override=frontend_override)

        if entry_path is None and frontend_dir is None:
            return None  # Not an app directory

        provenance: dict[str, str] = {}
        provenance["route_prefix"] = "convention: directory_name"

        if entry_path is not None:
            app_type, type_source = self._detect_app_type(
                entry_path, apps_dir, self.conventions.app_attr
            )
            provenance["app_type"] = type_source
            provenance["entry_module_path"] = (
                f"convention: first match ({entry_path.name})"
            )
        else:
            app_type = "frontend_only"
            entry_path = None
            provenance["app_type"] = "convention: no backend entry, has frontend"

        provenance["source_dir"] = str(apps_dir)

        config = AppConfig(
            name=name,
            route_prefix=route_prefix,
            entry_module_path=entry_path,
            app_type=app_type,
            app_attr=self.conventions.app_attr,
            frontend_dir=frontend_dir,
            source_dir=apps_dir,
            provenance=provenance,
        )

        # Apply per-app overrides (asgi-mode — remaining TOML fields)
        if toml_data:
            config = self._apply_overrides(config, app_dir, toml_data)

        return self._finalize_metadata(config, app_dir)

    def _finalize_metadata(self, config: AppConfig, app_dir: Path) -> AppConfig:
        """Bake app-declared launcher metadata onto a freshly discovered config.

        Runs for *every* app on *both* discovery paths (asgi and non-asgi), so
        external/process apps without a frontend on disk are covered too. The
        harvest reads the manifest / ``index.html`` head / ``package.json`` /
        ``pyproject`` / fs icon conventions; each source is a no-op when absent,
        so this never raises on an app that only has an ``app.toml`` (or nothing).
        """
        from enlace.appmeta import harvest_app_metadata

        updates = harvest_app_metadata(config, app_dir, config.frontend_dir)
        if updates:
            config = config.model_copy(update=updates)
        return config

    def _build_non_asgi_config(
        self,
        name: str,
        route_prefix: str,
        apps_dir: Path,
        app_dir: Path,
        toml_data: dict,
        declared_mode: str,
    ) -> AppConfig:
        """Build an AppConfig for process/external/static modes from app.toml.

        Skips Python introspection — the app may not be Python at all.
        """
        provenance: dict[str, str] = {
            "mode": "override: app.toml",
            "route_prefix": "convention: directory_name",
            "source_dir": str(apps_dir),
            "app_type": f"inferred from mode={declared_mode}",
        }

        # Infer app_type from mode
        if declared_mode == "static":
            app_type = "frontend_only"
        else:
            app_type = "asgi_app"  # process/external — opaque to enlace

        # Start with convention defaults, then overlay TOML fields
        fields: dict = dict(
            name=name,
            route_prefix=route_prefix,
            app_type=app_type,
            mode=declared_mode,
            source_dir=apps_dir,
            provenance=provenance,
        )

        # Apply all TOML fields (same mapping as _apply_overrides)
        fields, provenance = _overlay_toml_fields(
            fields,
            provenance,
            toml_data,
            app_dir,
        )
        fields["provenance"] = provenance
        return AppConfig(**fields)

    def _find_entry_point(self, app_dir: Path) -> Optional[Path]:
        """Find the first matching entry point in the app directory."""
        for name in self.conventions.entry_points:
            candidate = app_dir / name
            if candidate.is_file():
                return candidate
        return None

    def _find_frontend_dir(
        self, app_dir: Path, *, override: Optional[str] = None
    ) -> Optional[Path]:
        """Check if the app has a frontend assets directory.

        ``override`` is the ``frontend_dir`` value from app.toml (if any) and
        takes precedence over the convention. The directory must exist and
        contain an ``index.html``.
        """
        candidate_name = override if override else self.conventions.frontend_dir
        frontend = app_dir / candidate_name
        if frontend.is_dir() and (frontend / "index.html").exists():
            return frontend
        return None

    def _detect_app_type(
        self, entry_path: Path, apps_dir: Path, app_attr: str
    ) -> tuple[str, str]:
        """Detect whether the entry module provides an ASGI app or functions.

        Returns:
            Tuple of (app_type, provenance_source).

        Raises:
            ImportError: If the module exists but fails to import (e.g. syntax
                error, missing dependency). This is intentionally NOT caught —
                silently swallowing import errors is an anti-pattern.
        """
        module = _import_module_from_path(entry_path, apps_dir)

        # Check for ASGI app attribute
        if hasattr(module, app_attr):
            obj = getattr(module, app_attr)
            if callable(obj):
                return "asgi_app", f"detected: has '{app_attr}' attribute"

        # Check for typed public functions
        public_functions = [
            name
            for name, obj in inspect.getmembers(module, inspect.isfunction)
            if not name.startswith("_") and obj.__module__ == module.__name__
        ]
        if public_functions:
            return "functions", "detected: no app attr, has public functions"

        return "asgi_app", f"detected: has '{app_attr}' attribute (fallback)"

    def _apply_overrides(
        self,
        config: AppConfig,
        app_dir: Path,
        toml_data: dict,
    ) -> AppConfig:
        """Apply per-app TOML overrides to a discovered asgi-mode config."""
        updates: dict = {}
        provenance = dict(config.provenance)
        updates, provenance = _overlay_toml_fields(
            updates,
            provenance,
            toml_data,
            app_dir,
        )
        updates["provenance"] = provenance
        return config.model_copy(update=updates)

    def discover_app_dir(self, app_dir: Path) -> Optional[AppConfig]:
        """Discover a single directory that IS the app itself.

        Unlike discover(), which walks children of a container directory,
        this treats app_dir itself as the app directory.

        Args:
            app_dir: Path to the app directory (the directory IS the app).

        Returns:
            AppConfig if the directory is a valid app, None otherwise.
        """
        if not app_dir.exists() or not app_dir.is_dir():
            return None
        if is_skippable(app_dir.name):
            return None
        return self._discover_app(app_dir, app_dir.parent)


def _load_toml(path: Path) -> dict:
    """Load a TOML file, returning an empty dict on missing file."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        raise ValueError(f"Failed to parse {path}: {e}") from e


# Cross-cutting TOML keys that all strategies share. Mode-specific keys
# (``command``, ``port``, ``upstream_url``, ``public_dir``, ...) are
# contributed by each ``BackendStrategy`` and merged in by
# ``_resolved_field_map`` below.
_CORE_TOML_FIELD_MAP = {
    "route": "route_prefix",
    "access": "access",
    "shared_password_env": "shared_password_env",
    "allowed_users": "allowed_users",
    "display_name": "display_name",
    # `title` is an alias for `display_name`. If an app.toml sets BOTH, the
    # later key in this map wins (dict-insertion order in _overlay_toml_fields)
    # — declaring both is an authoring mistake; no app does.
    "title": "display_name",
    "frontend_dir": "frontend_dir",
    "mode": "mode",
    # App-launcher metadata (see enlace.appmeta). These are the app-declared
    # (Tier C.1) values; harvested sources fill the gaps at the tail of
    # _discover_app.
    "description": "description",
    "keywords": "keywords",
    "icon": "icon",
    "launchable": "launchable",
}

# Core path keys (resolved relative to the app directory). Strategy-specific
# path keys are contributed via ``BackendStrategy.path_keys``.
_CORE_PATH_KEYS = {"frontend_dir"}


def _resolved_field_map() -> tuple[dict[str, str], set[str]]:
    """Build the effective TOML field map by merging core + strategy maps.

    Recomputed per call so newly-registered plugin strategies are picked up
    without restarting the process. Cheap — the registry is small.
    """
    from enlace.strategies import collect_strategy_field_maps

    strategy_map, strategy_paths = collect_strategy_field_maps()
    field_map = {**_CORE_TOML_FIELD_MAP, **strategy_map}
    path_keys = _CORE_PATH_KEYS | strategy_paths
    return field_map, path_keys


def _overlay_toml_fields(
    fields: dict,
    provenance: dict,
    toml_data: dict,
    app_dir: Path,
) -> tuple[dict, dict]:
    """Apply TOML overrides to a fields dict and provenance dict.

    Returns the updated (fields, provenance) pair.
    """
    field_map, path_keys = _resolved_field_map()
    for toml_key, field_name in field_map.items():
        if toml_key not in toml_data:
            continue
        value = toml_data[toml_key]
        # Resolve relative paths
        if toml_key in path_keys:
            value = app_dir / value
        # command: accept string (split via shlex) or array
        if toml_key == "command" and isinstance(value, str):
            value = shlex.split(value)
        fields[field_name] = value
        provenance[field_name] = "override: app.toml"

    # The [build] table is structured (not a flat scalar), so it's parsed
    # separately into a BuildConfig rather than going through field_map.
    build_table = toml_data.get("build")
    if isinstance(build_table, dict):
        fields["build"] = _parse_build_config(build_table, app_dir)
        provenance["build"] = "override: app.toml [build]"

    return fields, provenance


def _parse_build_config(raw: dict, app_dir: Path):
    """Build a ``BuildConfig`` from an app.toml ``[build]`` table.

    ``cwd`` is resolved relative to the app directory here (it needs the app
    dir as context); command-string splitting is handled by the model.
    """
    from enlace.base import BuildConfig

    data = dict(raw)
    cwd = data.get("cwd")
    if cwd is not None:
        data["cwd"] = app_dir / cwd
    return BuildConfig(**data)


def _import_module_from_path(entry_path: Path, apps_dir: Path) -> object:
    """Import a module from a filesystem path.

    Adds the apps_dir parent to sys.path temporarily so that imports
    resolve correctly.

    Raises:
        ImportError: If the module has a genuine import error (syntax error,
            missing dependency). This is intentionally propagated.
    """
    root = str(apps_dir.parent.resolve())
    added_to_path = False
    if root not in sys.path:
        sys.path.insert(0, root)
        added_to_path = True
    try:
        # Build module path: apps/foo/server.py -> apps.foo.server
        relative = entry_path.resolve().relative_to(Path(root).resolve())
        module_name = str(relative.with_suffix("")).replace("/", ".").replace("\\", ".")

        # Remove cached module to ensure fresh import
        if module_name in sys.modules:
            del sys.modules[module_name]

        return importlib.import_module(module_name)
    finally:
        if added_to_path:
            try:
                sys.path.remove(root)
            except ValueError:
                pass


def discover_apps(config: Optional[PlatformConfig] = None) -> PlatformConfig:
    """High-level discovery: load config, discover apps, check conflicts.

    Iterates over all configured source directories:
    - ``config.apps_dirs``: container directories (walk children)
    - ``config.app_dirs``: individual app directories (discover directly)

    Args:
        config: Platform configuration. If None, loads from platform.toml.

    Returns:
        PlatformConfig with apps populated.

    Raises:
        RuntimeError: If name or route conflicts are detected.
    """
    if config is None:
        config = PlatformConfig.from_toml()
    discoverer = ConventionDiscoverer(config.conventions)

    all_apps: list[AppConfig] = []

    # Walk container directories
    for apps_dir in config.apps_dirs:
        all_apps.extend(discoverer.discover(apps_dir))

    # Discover individual app directories
    for app_dir in config.app_dirs:
        app_config = discoverer.discover_app_dir(app_dir)
        if app_config is not None:
            all_apps.append(app_config)

    # Sort globally by name for deterministic order
    all_apps.sort(key=lambda a: a.name)

    config = config.model_copy(update={"apps": all_apps})
    errors = config.check_conflicts()
    if errors:
        raise RuntimeError(
            "Configuration conflicts detected:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
    return config
