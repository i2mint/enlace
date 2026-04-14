"""Tests for enlace.supervise — dev-mode process supervisor."""

import asyncio
import sys
import time
from pathlib import Path

from enlace.supervise import ManagedProcess

# -- Helpers ------------------------------------------------------------------


def _make_proc(
    name="test_app",
    command=None,
    port=None,
    **kwargs,
):
    """Build a ManagedProcess with sensible test defaults."""
    if command is None:
        command = [sys.executable, "-c", "print('hello')"]
    return ManagedProcess(
        name=name,
        command=command,
        cwd=Path("."),
        port=port,
        **kwargs,
    )


def _http_server_command(port: int) -> list[str]:
    """Command to start a Python HTTP server on the given port."""
    return [
        sys.executable, "-c",
        f"from http.server import HTTPServer, BaseHTTPRequestHandler; "
        f"HTTPServer(('127.0.0.1', {port}), BaseHTTPRequestHandler)"
        f".serve_forever()",
    ]


def _find_free_port() -> int:
    """Find an available TCP port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


# -- Lifecycle tests ----------------------------------------------------------


def test_start_and_stop():
    """A process can be started and stopped cleanly."""
    async def go():
        proc = _make_proc(
            command=[sys.executable, "-c", "import time; time.sleep(60)"],
        )
        await proc.start()
        assert proc.state == "starting"
        assert proc.process is not None
        assert proc.process.returncode is None

        await proc.stop(timeout=5.0)
        assert proc.state == "exited"
        assert proc.process.returncode is not None

    _run(go())


def test_stop_already_exited():
    """Stopping a process that already exited is a no-op."""
    async def go():
        proc = _make_proc(command=[sys.executable, "-c", "pass"])
        await proc.start()
        await proc.process.wait()  # let it finish naturally
        await proc.stop()
        assert proc.state == "exited"

    _run(go())


# -- Health check tests -------------------------------------------------------


def test_health_check_passes():
    """Health check passes when a server is listening on the port."""
    port = _find_free_port()

    async def go():
        proc = _make_proc(
            command=_http_server_command(port),
            port=port,
            ready_timeout=10.0,
        )
        try:
            await proc.start()
            healthy = await proc.wait_healthy()
            assert healthy
            assert proc.state == "running"
        finally:
            await proc.stop()

    _run(go())


def test_health_check_no_port():
    """If no port is configured, health check passes immediately."""
    async def go():
        proc = _make_proc(
            command=[sys.executable, "-c", "import time; time.sleep(60)"],
            port=None,
        )
        try:
            await proc.start()
            healthy = await proc.wait_healthy()
            assert healthy
            assert proc.state == "running"
        finally:
            await proc.stop()

    _run(go())


def test_health_check_timeout():
    """Health check times out if the process doesn't listen on the port."""
    port = _find_free_port()

    async def go():
        proc = _make_proc(
            # Process that runs but doesn't listen on any port
            command=[sys.executable, "-c", "import time; time.sleep(60)"],
            port=port,
            ready_timeout=2.0,  # short timeout for test speed
        )
        try:
            await proc.start()
            healthy = await proc.wait_healthy()
            assert not healthy
        finally:
            await proc.stop()

    _run(go())


# -- Restart logic tests ------------------------------------------------------


def test_should_restart_on_failure():
    """With on-failure policy, non-zero exit triggers restart."""
    proc = _make_proc(restart_policy="on-failure", max_retries=3)

    class FakeProcess:
        returncode = 1
    proc.process = FakeProcess()

    assert proc.should_restart() is True


def test_should_not_restart_clean_exit():
    """With on-failure policy, exit code 0 does NOT trigger restart."""
    proc = _make_proc(restart_policy="on-failure")

    class FakeProcess:
        returncode = 0
    proc.process = FakeProcess()

    assert proc.should_restart() is False


def test_should_not_restart_never_policy():
    """With never policy, no restart regardless of exit code."""
    proc = _make_proc(restart_policy="never")

    class FakeProcess:
        returncode = 1
    proc.process = FakeProcess()

    assert proc.should_restart() is False


def test_should_restart_always_policy():
    """With always policy, even clean exit triggers restart."""
    proc = _make_proc(restart_policy="always", max_retries=3)

    class FakeProcess:
        returncode = 0
    proc.process = FakeProcess()

    assert proc.should_restart() is True


def test_max_retries_exceeded():
    """After max_retries consecutive failures, gives up and transitions to fatal."""
    proc = _make_proc(restart_policy="on-failure", max_retries=2)
    proc._consecutive_failures = 2

    class FakeProcess:
        returncode = 1
    proc.process = FakeProcess()

    assert proc.should_restart() is False
    assert proc.state == "fatal"


# -- Backoff tests ------------------------------------------------------------


def test_backoff_delay_increases():
    """Backoff delay increases exponentially with failures."""
    proc = _make_proc(restart_delay_ms=100)

    delays = []
    for i in range(5):
        proc._consecutive_failures = i
        delays.append(proc.backoff_delay())

    # Each should be >= previous (exponential growth)
    for i in range(1, len(delays)):
        assert delays[i] >= delays[i - 1]

    # First delay should be 0.1s (100ms)
    assert abs(delays[0] - 0.1) < 0.01


def test_backoff_delay_capped():
    """Backoff delay is capped at 15 seconds."""
    proc = _make_proc(restart_delay_ms=100)
    proc._consecutive_failures = 100  # very high
    assert proc.backoff_delay() == 15.0


def test_backoff_reset_after_stable():
    """Consecutive failures reset after 30s of stable uptime."""
    proc = _make_proc()
    proc._consecutive_failures = 3

    proc._started_at = time.monotonic() - 31.0  # 31 seconds ago
    proc.maybe_reset_backoff()
    assert proc._consecutive_failures == 0


def test_backoff_not_reset_too_soon():
    """Consecutive failures don't reset if uptime < 30s."""
    proc = _make_proc()
    proc._consecutive_failures = 3

    proc._started_at = time.monotonic() - 5.0  # only 5 seconds ago
    proc.maybe_reset_backoff()
    assert proc._consecutive_failures == 3


# -- Log streaming tests ------------------------------------------------------


def test_log_streaming(capsys):
    """Process stdout is captured and printed with name prefix."""
    async def go():
        proc = _make_proc(
            command=[sys.executable, "-c", "print('hello from child')"],
            color="\033[36m",
        )
        await proc.start()
        await proc.stream_logs()
        await proc.process.wait()

    _run(go())

    captured = capsys.readouterr()
    assert "hello from child" in captured.out
    assert "test_app" in captured.out
