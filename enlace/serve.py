"""Server orchestration for enlace.

Starts Uvicorn as a subprocess with the composed app factory, supporting
hot reload in development mode and graceful shutdown via signal forwarding.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from enlace.base import PlatformConfig

_children: list[subprocess.Popen] = []
_shutting_down = False


def _graceful_shutdown(signum, frame):
    """Forward termination signal to child processes and wait for exit."""
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True

    for proc in _children:
        if proc.poll() is None:
            proc.send_signal(signum)

    deadline = time.monotonic() + 30
    for proc in _children:
        remaining = max(0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    sys.exit(0)


def serve(
    *,
    mode: str = "dev",
    apps_dir: str = "",
    apps_dirs: str = "",
    app_dirs: str = "",
    port: int = 0,
    host: str = "",
    config: str = "platform.toml",
):
    """Start the enlace backend server.

    Reads platform.toml as the source of truth for directories and ports.
    CLI arguments override TOML values when provided.

    Args:
        mode: 'dev' for development (hot reload) or 'prod' for production.
        apps_dir: Path to the apps directory (backward compat, overrides TOML).
        apps_dirs: Comma-separated container directories (overrides TOML).
        app_dirs: Comma-separated individual app directories (overrides TOML).
        port: Port to listen on (0 = use TOML value or default 8000).
        host: Host to bind to (empty = use default 127.0.0.1).
        config: Path to platform.toml.
    """
    # Load platform config from TOML as the base
    platform = PlatformConfig.from_toml(Path(config))

    # CLI args override TOML values when explicitly provided
    if apps_dir or apps_dirs:
        all_apps_dirs: list[str] = []
        if apps_dir:
            all_apps_dirs.append(apps_dir)
        if apps_dirs:
            all_apps_dirs.extend(d.strip() for d in apps_dirs.split(",") if d.strip())
    else:
        all_apps_dirs = [str(d) for d in platform.apps_dirs]

    if app_dirs:
        all_app_dirs = [d.strip() for d in app_dirs.split(",") if d.strip()]
    else:
        all_app_dirs = [str(d) for d in platform.app_dirs]

    effective_port = port if port else platform.backend_port
    effective_host = host if host else "127.0.0.1"

    # Set env vars for the subprocess (read by PlatformConfig.from_toml)
    os.environ["ENLACE_APPS_DIRS"] = os.pathsep.join(all_apps_dirs)
    os.environ["ENLACE_APP_DIRS"] = os.pathsep.join(all_app_dirs)
    # Legacy env var
    os.environ["ENLACE_APPS_DIR"] = all_apps_dirs[0] if all_apps_dirs else ""

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "enlace.compose:create_app",
        "--factory",
        "--host",
        effective_host,
        "--port",
        str(effective_port),
    ]

    if mode == "dev":
        cmd.append("--reload")
        for d in all_apps_dirs + all_app_dirs:
            if Path(d).exists():
                cmd += ["--reload-dir", d]
    else:
        cmd += ["--workers", "2", "--timeout-graceful-shutdown", "25"]

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    proc = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)
    _children.append(proc)

    try:
        proc.wait()
    except KeyboardInterrupt:
        _graceful_shutdown(signal.SIGINT, None)
