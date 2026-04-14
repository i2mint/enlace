"""Dev-mode process supervisor for enlace.

Spawns, health-checks, restarts (with exponential backoff), and streams logs
for process-mode apps.  Built on ``asyncio.create_subprocess_exec`` with no
external dependencies.

This is for interactive development (``enlace serve`` / ``enlace dev``).
Production process management is delegated to systemd.
"""

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# -- ANSI color helpers -------------------------------------------------------

_COLORS = [
    "\033[36m",  # cyan
    "\033[33m",  # yellow
    "\033[35m",  # magenta
    "\033[32m",  # green
    "\033[34m",  # blue
    "\033[31m",  # red
]
_RESET = "\033[0m"


def _color_for(index: int) -> str:
    return _COLORS[index % len(_COLORS)]


# -- ManagedProcess -----------------------------------------------------------


@dataclass
class ManagedProcess:
    """A supervised child process with health checking and restart logic."""

    name: str
    command: list[str]
    cwd: Path
    port: Optional[int] = None
    socket_path: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)
    health_check_path: str = "/health"
    ready_timeout: float = 30.0
    restart_policy: str = "on-failure"  # always | on-failure | never
    max_retries: int = 5
    restart_delay_ms: int = 100
    color: str = ""

    # Runtime state (not constructor args)
    state: str = field(default="stopped", repr=False)
    process: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    _consecutive_failures: int = field(default=0, repr=False)
    _started_at: Optional[float] = field(default=None, repr=False)

    # ---- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Spawn the child process."""
        self.state = "starting"
        child_env = {**os.environ, **self.env, "ENLACE_MANAGED": "1"}
        if self.port is not None:
            child_env["PORT"] = str(self.port)

        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # own process group for clean kill
        )
        self._started_at = time.monotonic()
        self._log(f"started (pid {self.process.pid})")

    async def stop(self, timeout: float = 10.0) -> None:
        """Gracefully stop: SIGTERM, wait, SIGKILL if needed."""
        if self.process is None or self.process.returncode is not None:
            self.state = "exited"
            return

        self.state = "stopping"
        try:
            pgid = os.getpgid(self.process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            self.state = "exited"
            return

        try:
            await asyncio.wait_for(self.process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._log("did not stop in time, sending SIGKILL")
            try:
                pgid = os.getpgid(self.process.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            await self.process.wait()

        self.state = "exited"
        self._log(f"stopped (exit code {self.process.returncode})")

    # ---- health checking ----------------------------------------------------

    async def wait_healthy(self) -> bool:
        """Poll until the process is accepting TCP connections or timeout."""
        if self.port is None:
            # No port to check — assume ready immediately
            self.state = "running"
            return True

        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.returncode is not None:
                return False  # process exited before becoming healthy
            if await self._tcp_ready():
                self.state = "running"
                self._consecutive_failures = 0
                self._started_at = time.monotonic()
                self._log("healthy")
                return True
            await asyncio.sleep(0.5)

        self._log(f"not healthy after {self.ready_timeout}s")
        return False

    async def _tcp_ready(self) -> bool:
        """Check if the port is accepting TCP connections (pure stdlib)."""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.port),
                timeout=1.0,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    # ---- restart logic ------------------------------------------------------

    def should_restart(self) -> bool:
        """Decide whether to restart after exit."""
        if self.process is None:
            return False
        rc = self.process.returncode
        if self.restart_policy == "never":
            return False
        if self.restart_policy == "on-failure" and rc == 0:
            return False
        if self._consecutive_failures >= self.max_retries:
            self._log(f"max retries ({self.max_retries}) exceeded, giving up")
            self.state = "fatal"
            return False
        return True

    def backoff_delay(self) -> float:
        """Compute exponential backoff delay in seconds."""
        delay = (self.restart_delay_ms / 1000.0) * (1.5**self._consecutive_failures)
        return min(delay, 15.0)  # cap at 15 seconds

    def record_failure(self) -> None:
        self._consecutive_failures += 1

    def maybe_reset_backoff(self) -> None:
        """Reset backoff if process has been stable for 30 seconds."""
        if self._started_at is not None and time.monotonic() - self._started_at > 30.0:
            self._consecutive_failures = 0

    # ---- log streaming ------------------------------------------------------

    async def stream_logs(self) -> None:
        """Read stdout line-by-line and print with colored name prefix."""
        if self.process is None or self.process.stdout is None:
            return
        label = f"{self.color}{self.name:>15}{_RESET}"
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            print(f"{label} | {text}", flush=True)

    # ---- helpers ------------------------------------------------------------

    def _log(self, msg: str) -> None:
        label = f"{self.color}{self.name:>15}{_RESET}"
        print(f"{label} | [enlace] {msg}", flush=True)


# -- Supervisor ---------------------------------------------------------------


async def _supervise_one(proc: ManagedProcess) -> None:
    """Lifecycle loop for a single managed process: start, stream, restart."""
    while True:
        await proc.start()

        # Run log streaming and health check concurrently
        log_task = asyncio.create_task(proc.stream_logs())
        healthy = await proc.wait_healthy()

        if not healthy and proc.process and proc.process.returncode is None:
            await proc.stop()
            log_task.cancel()
            if not proc.should_restart():
                return
            proc.record_failure()
            delay = proc.backoff_delay()
            proc._log(f"restarting in {delay:.1f}s")
            await asyncio.sleep(delay)
            continue

        # Wait for process to exit
        await proc.process.wait()
        await log_task  # drain remaining output

        proc.maybe_reset_backoff()

        rc = proc.process.returncode
        proc._log(f"exited with code {rc}")

        if not proc.should_restart():
            proc.state = "exited" if proc.state != "fatal" else "fatal"
            return

        proc.record_failure()
        delay = proc.backoff_delay()
        proc._log(f"restarting in {delay:.1f}s")
        await asyncio.sleep(delay)


async def supervise_all(
    processes: list[ManagedProcess],
) -> None:
    """Spawn and supervise all managed processes until shutdown.

    Registers SIGTERM/SIGINT handlers for graceful shutdown.
    """
    if not processes:
        return

    shutdown_event = asyncio.Event()

    def _request_shutdown():
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown)

    # Assign colors
    for i, proc in enumerate(processes):
        proc.color = _color_for(i)

    # Start all supervision tasks
    tasks = [asyncio.create_task(_supervise_one(p)) for p in processes]

    # Wait for shutdown signal or all tasks to complete
    shutdown_waiter = asyncio.create_task(shutdown_event.wait())
    done, pending = await asyncio.wait(
        [*tasks, shutdown_waiter],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # If shutdown was requested, stop everything
    if shutdown_event.is_set():
        for task in tasks:
            task.cancel()
        await asyncio.gather(
            *(proc.stop() for proc in processes),
            return_exceptions=True,
        )
    else:
        # A supervision task ended naturally; cancel the shutdown waiter
        shutdown_waiter.cancel()

    # Cancel any remaining tasks
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
