"""Core data structures for enlace platform configuration."""

import os
import sys
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

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


class AppConfig(BaseModel):
    """Resolved configuration for a single discovered app."""

    name: str
    route_prefix: str
    entry_module_path: Optional[Path] = None
    app_type: Literal["asgi_app", "functions", "frontend_only"]
    app_attr: str = "app"
    frontend_dir: Optional[Path] = None
    source_dir: Optional[Path] = None
    access: str = "local"
    display_name: str = ""
    provenance: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _default_display_name(self):
        if not self.display_name:
            self.display_name = self.name.replace("_", " ").title()
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

    domain: str = "localhost"
    backend_port: int = 8000
    frontend_port: int = 3000
    conventions: ConventionsConfig = Field(default_factory=ConventionsConfig)
    apps: list[AppConfig] = Field(default_factory=list)

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

        Reads environment variables as overrides:
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

        return errors
