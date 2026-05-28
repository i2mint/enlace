"""Server orchestration for enlace.

Starts Uvicorn as a subprocess with the composed app factory, supporting
hot reload in development mode and graceful shutdown via signal forwarding.

When process-mode apps are present, runs an asyncio event loop that
supervises both the gateway Uvicorn and all process-mode children.
"""

import asyncio
import errno
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from enlace.base import AppConfig, PlatformConfig
from enlace.discover import discover_apps

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


def _check_port_available(host: str, port: int) -> None:
    """Check if the port is available before starting uvicorn.

    Raises SystemExit with an actionable error message if the port is in use.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(
                f"\nERROR: Address {host}:{port} is already in use.\n"
                f"\n"
                f"  To see what's using it:\n"
                f"    lsof -i :{port}\n"
                f"\n"
                f"  To kill whatever is on that port:\n"
                f"    kill $(lsof -t -i :{port})\n"
                f"\n"
                f"  Or run enlace on a different port:\n"
                f"    enlace serve --port {port + 1}\n",
                file=sys.stderr,
            )
            raise SystemExit(1)
        raise
    finally:
        sock.close()


def _build_uvicorn_cmd(
    effective_host: str,
    effective_port: int,
    mode: str,
    reload_dirs: list[str],
) -> list[str]:
    """Build the Uvicorn command list."""
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
        for d in reload_dirs:
            if Path(d).exists():
                cmd += ["--reload-dir", d]
    else:
        cmd += ["--workers", "2", "--timeout-graceful-shutdown", "25"]

    return cmd


def _auto_allocate_ports(
    port_apps: list[AppConfig],
    start_port: int,
) -> list[AppConfig]:
    """Assign ports to ``needs_port`` apps that don't declare one."""
    result = []
    next_port = start_port
    for app in port_apps:
        if app.port is None and app.socket is None:
            app = app.model_copy(update={"port": next_port})
            next_port += 1
        result.append(app)
    return result


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

    # Run discovery so we can see which apps need supervised lifecycles.
    discovered = discover_apps(platform)

    reload_dirs = all_apps_dirs + all_app_dirs

    # Ask each strategy (not a hardcoded mode string) two things:
    #   needs_port      → enlace should auto-allocate a free port if unset
    #   is_supervisable → enlace runs a supervised lifecycle for it
    # Plugins (e.g. enlace_docker) opt in via these flags — no core change.
    from enlace.strategies import get_strategy

    port_apps = [a for a in discovered.apps if get_strategy(a.mode).needs_port]
    supervised_apps: list[AppConfig] = [
        app for app in discovered.apps if get_strategy(app.mode).is_supervisable
    ]

    if not supervised_apps:
        # Pure-asgi path — exactly the current behavior.
        _serve_asgi_only(effective_host, effective_port, mode, reload_dirs)
    else:
        # Allocate ports for needs_port apps that didn't declare one.
        if port_apps:
            port_apps = _auto_allocate_ports(port_apps, platform.process_port_start)
            _set_port_env(port_apps)
            # Re-attach allocated ports to the supervised_apps list so each
            # strategy's make_lifecycle sees the assigned port.
            ports_by_name = {a.name: a for a in port_apps}
            supervised_apps = [ports_by_name.get(a.name, a) for a in supervised_apps]
        _serve_mixed(
            effective_host,
            effective_port,
            mode,
            reload_dirs,
            platform,
            supervised_apps,
        )


def _serve_asgi_only(
    host: str,
    port: int,
    mode: str,
    reload_dirs: list[str],
) -> None:
    """Original serve path: single Uvicorn subprocess."""
    _check_port_available(host, port)
    cmd = _build_uvicorn_cmd(host, port, mode, reload_dirs)

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    proc = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)
    _children.append(proc)

    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        _graceful_shutdown(signal.SIGINT, None)
        rc = proc.returncode or 0

    sys.exit(rc)


def _serve_mixed(
    host: str,
    port: int,
    mode: str,
    reload_dirs: list[str],
    platform: PlatformConfig,
    supervised_apps: list[AppConfig],
) -> None:
    """Mixed-mode serve: asyncio supervisor + gateway Uvicorn.

    Each supervised app's lifecycle is built via its registered
    ``BackendStrategy.make_lifecycle`` — the supervisor does not know
    whether it's driving a subprocess (``ProcessStrategy``) or, e.g., a
    docker container (an ``enlace_docker`` plugin strategy).
    """
    _check_port_available(host, port)
    from enlace.strategies import get_strategy
    from enlace.supervise import ManagedProcess, supervise_all

    async def _run():
        # Print startup summary
        print(f"\n{'=' * 60}")
        print("  enlace — multi-process mode")
        print(f"{'=' * 60}")
        print(f"  Gateway:  http://{host}:{port}")
        for app in supervised_apps:
            addr = f"http://127.0.0.1:{app.port}" if app.port else app.socket
            print(f"  {app.name:>12}:  {addr}  (mode={app.mode})")
        print(f"{'=' * 60}\n")

        # Each strategy turns its AppConfig into a Lifecycle.
        managed = []
        for app in supervised_apps:
            strategy = get_strategy(app.mode)
            lifecycle = strategy.make_lifecycle(app, platform)
            if lifecycle is not None:
                managed.append(lifecycle)

        # Also manage the gateway Uvicorn as a supervised process. The
        # gateway is enlace's own concern (not strategy-driven) — it's the
        # uvicorn child that hosts all the mounted/proxied sub-apps.
        uvicorn_cmd = _build_uvicorn_cmd(host, port, mode, reload_dirs)
        gateway = ManagedProcess(
            name="gateway",
            command=uvicorn_cmd,
            cwd=Path("."),
            port=port,
            ready_timeout=15.0,
            restart_policy="on-failure",
        )
        all_processes = [gateway] + managed

        await supervise_all(all_processes)

    asyncio.run(_run())


def _set_port_env(port_apps: list[AppConfig]) -> None:
    """Set env vars so compose.py can read auto-allocated ports.

    For each ``needs_port`` app, we store the port so that build_backend()
    can create the correct proxy routes. This is done via env vars because
    the gateway runs in a subprocess.
    """
    port_map = {}
    for app in port_apps:
        if app.port is not None:
            port_map[app.name] = str(app.port)
    if port_map:
        import json

        os.environ["ENLACE_PROCESS_PORTS"] = json.dumps(port_map)
