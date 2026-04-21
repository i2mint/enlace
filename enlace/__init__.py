"""enlace -- Compose, serve, and deploy multiple web apps from a single codebase.

Drop a Python module or a React app into a directory, and enlace discovers it,
mounts it, serves it, and optionally gates it behind auth -- with zero boilerplate.
"""

from pathlib import Path

from enlace.base import (
    AppConfig,
    AuthConfig,
    ConventionsConfig,
    OAuthProviderConfig,
    PlatformConfig,
    StoreBackendConfig,
)
from enlace.compose import EnlaceConfigError, build_backend, create_app
from enlace.diagnose import DiagnosticReport, Issue, diagnose_app
from enlace.discover import ConventionDiscoverer, discover_apps
from enlace.serve import serve

__version__ = "0.0.1"

__all__ = [
    "AppConfig",
    "AuthConfig",
    "ConventionsConfig",
    "DiagnosticReport",
    "Issue",
    "OAuthProviderConfig",
    "PlatformConfig",
    "StoreBackendConfig",
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
