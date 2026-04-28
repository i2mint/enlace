"""enlace -- Compose, serve, and deploy multiple web apps from a single codebase.

Drop a Python module or a React app into a directory, and enlace discovers it,
mounts it, serves it, and optionally gates it behind auth -- with zero boilerplate.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version
from pathlib import Path

from enlace.base import (
    AppConfig,
    ConventionsConfig,
    PlatformConfig,
)
from enlace.compose import EnlaceConfigError, Plugin, build_backend, create_app
from enlace.diagnose import DiagnosticReport, Issue, diagnose_app
from enlace.discover import ConventionDiscoverer, discover_apps
from enlace.serve import serve

try:
    __version__ = _version("enlace")
except PackageNotFoundError:  # editable install with no metadata, etc.
    __version__ = "0.0.0+local"

__all__ = [
    "AppConfig",
    "ConventionsConfig",
    "DiagnosticReport",
    "Issue",
    "PlatformConfig",
    "Plugin",
    "ConventionDiscoverer",
    "EnlaceConfigError",
    "build_backend",
    "create_app",
    "diagnose_app",
    "discover_apps",
    "serve",
    "skills_dir",
]


def skills_dir() -> Path:
    """Return the path to this package's bundled skills directory."""
    return Path(__file__).parent / "data" / "skills"
