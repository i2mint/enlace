"""Core data structures for enlace platform configuration.

enlace does not enforce access — that's ``enlace_auth``'s job. But enlace
*does* read ``AppConfig.access`` and ``AppConfig.allowed_users`` for one
narrow purpose: filtering the ``/_apps`` listing so authenticated users
don't see entries they couldn't open anyway. That makes the access string
vocabulary part of enlace's contract — the values
``"public" | "local" | "protected:shared" | "protected:user"`` are the
ones ``compose._can_access`` understands; anything else is treated as
deny-by-default.

The fields are otherwise opaque to enlace: enforcement, session lookup,
allowlist matching at request time, and CSRF all live in the
``enlace_auth`` plugin (passed in via ``build_backend(..., plugins=[...])``).

Likewise, ``[auth.*]`` and ``[stores.*]`` tables in ``platform.toml`` are
preserved as untyped dicts on ``PlatformConfig`` so plugins can deserialize
them with their own models.
"""

import os
import shlex
import sys
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enlace.appmeta import AppMetaConfig

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]


class ConventionsConfig(BaseModel):
    """Meta-conventions controlling how apps are discovered."""

    entry_points: list[str] = Field(
        default=["server.py", "app.py", "main.py"],
        description="Ordered list of filenames to search for as backend entry points",
    )
    app_attr: str = Field(
        default="app",
        description="Attribute name to look up on the entry module for an ASGI app",
    )
    frontend_dir: str = Field(
        default="frontend",
        description="Subdirectory name containing frontend assets",
    )


class BuildConfig(BaseModel):
    """Declarative build instructions for an app's compiled frontend.

    Mirrors the ``[build]`` table in ``app.toml``. It describes the app, not
    its deployment — a Vite/Next/esbuild app has a ``build`` command whether
    or not enlace serves it — so it keeps the "apps don't know about enlace"
    principle intact. enlace (or any deployer) runs these as an explicit step
    via ``enlace build``; enlace never builds at request time.

    ``install`` / ``build`` accept either a single string (split with
    ``shlex``) or a list of argv tokens. ``cwd`` is resolved relative to the
    app directory at discovery time; ``None`` means "the app directory".
    ``env_vars`` is a *hint* of which env vars the build honours (e.g.
    ``VITE_API_BASE``) so deployers know what they may inject — enlace does
    not choose values. ``outputs`` is an optional hint of produced paths.
    """

    cwd: Optional[Path] = None
    install: Optional[list[str]] = None
    build: Optional[list[str]] = None
    env_vars: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)

    @field_validator("install", "build", mode="before")
    @classmethod
    def _split_commands(cls, v):
        """Accept a shell string or an argv list for command fields."""
        if isinstance(v, str):
            return shlex.split(v)
        return v


class AppImportError(BaseModel):
    """Why an app's entry module could not be imported at discovery time.

    Recorded (instead of raised) when discovery runs with
    ``on_import_error="record"`` — see :func:`enlace.discover.discover_apps`.
    Importing a module runs arbitrary module-level code, so ``exception_type``
    is not necessarily an ``ImportError``: the failure seen in production was a
    ``PermissionError`` from a dependency reading a root-only dotenv at import.
    """

    exception_type: str
    message: str
    entry_module_path: Optional[Path] = None

    @classmethod
    def from_exception(
        cls, exc: BaseException, entry_module_path: Optional[Path] = None
    ) -> "AppImportError":
        """Build a record from the exception the import raised."""
        return cls(
            exception_type=type(exc).__name__,
            message=str(exc),
            entry_module_path=entry_module_path,
        )

    def __str__(self) -> str:
        return f"{self.exception_type}: {self.message}"


class AppConfig(BaseModel):
    """Resolved configuration for a single discovered app.

    ``extra="allow"`` lets plugin strategies (e.g. ``enlace_docker``) carry
    their own typed fields — ``dockerfile``, ``image``, ``compose_file``,
    ``service``, etc. — without enlace having to enumerate them. The
    plugin's ``BackendStrategy`` reads them via attribute access
    (``app.dockerfile``); enlace itself ignores them.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    route_prefix: str
    entry_module_path: Optional[Path] = None
    app_type: Literal["asgi_app", "functions", "frontend_only"]
    app_attr: str = "app"
    frontend_dir: Optional[Path] = None
    source_dir: Optional[Path] = None
    # Declarative frontend build (from app.toml's [build] table). Mode-agnostic
    # — any app with a compiled frontend can declare it. Run via `enlace build`
    # as an explicit pre-deploy step; never at request time. See BuildConfig.
    build: Optional[BuildConfig] = None
    # Auth policy field. Consumed by enlace_auth (if installed) — enlace
    # itself does not interpret this. Free-form string to avoid coupling
    # enlace's data model to auth's vocabulary; enlace_auth normalizes it.
    access: str = "local"
    shared_password_env: Optional[str] = None
    allowed_users: list[str] = Field(
        default_factory=list,
        description=(
            "Restrict access to these specific user emails. When non-empty, "
            "requests authenticated as anyone else are rejected (for "
            "access=protected:user apps) or the app is hidden (from /_apps). "
            "Empty list means any authenticated user allowed."
        ),
    )
    display_name: str = ""
    provenance: dict[str, str] = Field(default_factory=dict)

    # Set only when discovery ran with ``on_import_error="record"`` and this
    # app's entry module raised on import. ``None`` on every healthy app, and
    # on every app discovered under the default ``"raise"`` policy (which
    # never gets far enough to build a config for a broken app).
    import_error: Optional[AppImportError] = None

    # App-launcher metadata. Resolved at discovery from app.toml + harvested
    # sources (manifest, index.html <head>, package.json, pyproject) — see
    # enlace.appmeta. `keywords` is the union of every app-declared source;
    # `icon` is an app-dir-relative path, an emoji/glyph, a data: URI, an
    # absolute https URL, or "" (⇒ a generated monogram at serve time).
    # `launchable=None` means "derive from mode" (see compose._app_launch).
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    icon: str = ""
    launchable: Optional[bool] = None

    # When this app's source last changed (ISO-8601), for the launcher's "last
    # updated" sort. NOT declared in app.toml — it is stamped onto the config at
    # startup from the app's deploy manifest (``SourceRef.committed_at``), because
    # only the deploy tool can see the git history. ``None`` when there is no
    # manifest yet, which the launcher renders as "unknown" and sorts last.
    updated_at: Optional[str] = None

    # Mode: how this app is served (orthogonal to app_type which is what was
    # detected). Free-form string validated against the strategy registry —
    # see ``enlace.strategies`` for the open/closed extension point.
    mode: str = "asgi"

    # Process-mode fields
    command: Optional[list[str]] = None
    port: Optional[int] = None
    socket: Optional[str] = None
    env: dict[str, str] = Field(default_factory=dict)
    health_check_path: str = "/health"
    ready_timeout: float = 30.0
    restart_policy: Literal["always", "on-failure", "never"] = "on-failure"
    max_retries: int = 5
    restart_delay_ms: int = 100

    # External-mode fields
    upstream_url: Optional[str] = None

    # Static-mode fields
    public_dir: Optional[Path] = None

    @model_validator(mode="after")
    def _default_display_name(self):
        if not self.display_name:
            self.display_name = self.name.replace("_", " ").title()
        return self

    @model_validator(mode="after")
    def _validate_mode_fields(self):
        """Delegate per-mode validation to the registered strategy.

        The strategy decides what fields are required for its mode. Unknown
        modes surface as a ``ValueError`` from the registry with an
        actionable message (e.g. \"install enlace_docker for docker\").
        """
        # Lazy import: enlace.strategies must not be a hard dependency of
        # enlace.base — strategies imports base via TYPE_CHECKING only.
        from enlace.strategies import get_strategy

        strategy = get_strategy(self.mode)
        strategy.validate(self)
        return self


class PlatformConfig(BaseModel):
    """Resolved configuration for the entire platform."""

    # Deprecated scalar — folded into apps_dirs by the validator below.
    apps_dir: Optional[Path] = Field(default=None, exclude=True)

    # Directories that CONTAIN app subdirectories (walk children).
    apps_dirs: list[Path] = Field(default_factory=list)

    # Individual directories that ARE apps (discover directly).
    app_dirs: list[Path] = Field(default_factory=list)

    # Directory containing shared static assets (e.g. shared.css) served at /.
    shared_assets_dir: Optional[Path] = None

    # Directory where the deploy tool drops {app}.json build-identity files.
    # When unset, enlace's /_meta endpoints serve minimal stubs (app name +
    # enlace_version) so the diagnostic plumbing exists from day one. See
    # enlace.manifest for the manifest schema.
    manifest_dir: Optional[Path] = None

    index_page: bool = Field(
        default=True,
        description="Serve an auto-generated index page at / listing all apps",
    )
    landing_app: Optional[str] = Field(
        default=None,
        description=(
            "Name of a discovered app whose frontend should serve /. "
            "Overrides the built-in index_page. The app's frontend assets "
            "replace the default enlace index."
        ),
    )
    domain: str = "localhost"
    backend_port: int = 8000
    frontend_port: int = 3000
    process_port_start: int = 9100
    socket_dir: Path = Field(default=Path("/tmp/enlace"))
    conventions: ConventionsConfig = Field(default_factory=ConventionsConfig)
    apps: list[AppConfig] = Field(default_factory=list)
    # Auth + stores configuration is preserved as untyped dicts so plugins
    # (e.g. enlace_auth) can deserialize them with their own pydantic models
    # without enlace having to know the schema.
    auth: dict[str, Any] = Field(default_factory=dict)
    stores: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # App-launcher metadata config (platform.toml [app_meta]). `default_icon`
    # and `apps` (Tier B static overrides) are read by enlace core; `editors`
    # and `store_path` are carried for the enlace_auth plugin (the editable
    # overlay's authz + persistence), which enlace core never interprets.
    app_meta: AppMetaConfig = Field(default_factory=AppMetaConfig)

    @model_validator(mode="after")
    def _normalize_dirs(self):
        """Merge deprecated apps_dir into apps_dirs for backward compat."""
        if self.apps_dir is not None and self.apps_dir not in self.apps_dirs:
            self.apps_dirs.insert(0, self.apps_dir)
        if not self.apps_dirs and not self.app_dirs:
            self.apps_dirs = [Path("apps")]
        return self

    @property
    def all_source_dirs(self) -> list[Path]:
        """All directories to watch (for reload, etc.)."""
        return list(self.apps_dirs) + list(self.app_dirs)

    @classmethod
    def from_toml(cls, path: Path = Path("platform.toml")) -> "PlatformConfig":
        """Load configuration from a TOML file, falling back to defaults.

        Relative ``apps_dirs`` / ``app_dirs`` / ``shared_assets_dir`` entries
        in the TOML are resolved against the **TOML file's own directory**,
        not the process working directory. This makes a platform config
        host-portable: the same file works wherever the repo is checked out,
        as long as sibling app repos keep their relative layout. Absolute
        (and ``~``-prefixed) paths are left untouched.

        Reads environment variables as overrides (applied *after* relative
        resolution, so env values are taken verbatim — host-specific by
        design):
        - ENLACE_APPS_DIRS (pathsep-delimited): container directories
        - ENLACE_APP_DIRS (pathsep-delimited): individual app directories
        - ENLACE_APPS_DIR (legacy): single container directory

        Args:
            path: Path to platform.toml. If the file doesn't exist, returns
                  a PlatformConfig with all default values.
        """
        if not path.exists():
            data = {}
        else:
            try:
                with open(path, "rb") as f:
                    data = tomllib.load(f)
            except Exception as e:
                raise ValueError(f"Failed to parse {path}: {e}") from e

        platform_data = data.get("platform", {})
        conventions_data = data.get("conventions", {})
        if conventions_data:
            platform_data["conventions"] = conventions_data

        # Auth and stores tables are forwarded verbatim as dicts; plugins
        # such as enlace_auth deserialize them with their own models.
        auth_data = data.get("auth")
        if auth_data is not None:
            platform_data["auth"] = auth_data
        stores_data = data.get("stores")
        if stores_data is not None:
            platform_data["stores"] = stores_data
        # [app_meta] table — the app-launcher metadata config.
        app_meta_data = data.get("app_meta")
        if app_meta_data is not None:
            platform_data["app_meta"] = app_meta_data

        # Resolve relative path-like fields against the TOML file's own
        # directory (not the CWD), so the config is host-portable. Done
        # before env overrides — env values are intentionally verbatim.
        base_dir = path.resolve().parent

        def _resolve(value: Any) -> Path:
            p = Path(value).expanduser()
            return p if p.is_absolute() else (base_dir / p).resolve()

        for key in ("apps_dirs", "app_dirs"):
            if key in platform_data:
                platform_data[key] = [_resolve(d) for d in platform_data[key]]
        for key in ("shared_assets_dir", "apps_dir", "manifest_dir"):
            if key in platform_data:
                platform_data[key] = _resolve(platform_data[key])
        # [app_meta].store_path is path-like too; resolve it against the TOML
        # dir for the same host-portability reason (a ~-prefixed/absolute value
        # is left as-is by _resolve).
        app_meta = platform_data.get("app_meta")
        if isinstance(app_meta, dict) and app_meta.get("store_path"):
            app_meta["store_path"] = _resolve(app_meta["store_path"])

        # Environment variable overrides
        env_apps_dirs = os.environ.get("ENLACE_APPS_DIRS", "")
        if env_apps_dirs:
            platform_data["apps_dirs"] = [
                d for d in env_apps_dirs.split(os.pathsep) if d
            ]
        env_app_dirs = os.environ.get("ENLACE_APP_DIRS", "")
        if env_app_dirs:
            platform_data["app_dirs"] = [d for d in env_app_dirs.split(os.pathsep) if d]
        env_apps_dir = os.environ.get("ENLACE_APPS_DIR", "")
        if env_apps_dir and "apps_dirs" not in platform_data:
            platform_data["apps_dir"] = env_apps_dir

        return cls(**platform_data)

    def check_conflicts(self) -> list[str]:
        """Check for name and route conflicts across all apps.

        Returns all conflicts found (not just the first), so the user can fix
        them all at once.
        """
        errors: list[str] = []

        # Check duplicate app names (across sources)
        names: dict[str, str] = {}
        for app in self.apps:
            source = str(app.source_dir) if app.source_dir else "unknown"
            if app.name in names:
                errors.append(
                    f"Name conflict: '{app.name}' found in both "
                    f"'{names[app.name]}' and '{source}'"
                )
            else:
                names[app.name] = source

        # Check duplicate route prefixes
        routes: dict[str, str] = {}
        for app in self.apps:
            if app.route_prefix in routes:
                errors.append(
                    f"Route conflict: '{app.route_prefix}' claimed by both "
                    f"'{routes[app.route_prefix]}' and '{app.name}'"
                )
            else:
                routes[app.route_prefix] = app.name

        # Check duplicate ports among process-mode apps
        ports: dict[int, str] = {}
        for app in self.apps:
            if app.mode == "process" and app.port is not None:
                if app.port in ports:
                    errors.append(
                        f"Port conflict: port {app.port} claimed by both "
                        f"'{ports[app.port]}' and '{app.name}'"
                    )
                else:
                    ports[app.port] = app.name

        return errors
