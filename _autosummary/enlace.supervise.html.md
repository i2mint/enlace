# enlace.supervise

Dev-mode process supervisor for enlace.

Spawns, health-checks, restarts (with exponential backoff), and streams logs
for process-mode apps.  Built on `asyncio.create_subprocess_exec` with no
external dependencies.

This is for interactive development (`enlace serve` / `enlace dev`).
Production process management is delegated to systemd.

### Functions

| [`supervise_all`](#enlace.supervise.supervise_all)(processes)   | Spawn and supervise all managed processes until shutdown.   |
|-----------------------------------------------------------------------------|-------------------------------------------------------------|

### Classes

| [`ManagedProcess`](#enlace.supervise.ManagedProcess)(name, command, cwd[, port, ...])   | A supervised child process with health checking and restart logic.   |
|----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------|

### *class* enlace.supervise.ManagedProcess(name, command, cwd, port=None, socket_path=None, env=<factory>, health_check_path='/health', ready_timeout=30.0, restart_policy='on-failure', max_retries=5, restart_delay_ms=100, color='', state='stopped', process=None, \_consecutive_failures=0, \_started_at=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A supervised child process with health checking and restart logic.

#### backoff_delay()

Compute exponential backoff delay in seconds.

* **Return type:**
  [`float`](https://docs.python.org/3/builtins/functions.html#float)

#### is_alive()

Whether the underlying process is still running.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

#### log(msg)

Print a labeled status line. Public — part of the Lifecycle protocol.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### maybe_reset_backoff()

Reset backoff if process has been stable for 30 seconds.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### should_restart()

Decide whether to restart after exit.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

#### *async* start()

Spawn the child process.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### *async* stop(timeout=10.0)

Gracefully stop: SIGTERM, wait, SIGKILL if needed.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### *async* stream_logs()

Read stdout line-by-line and print with colored name prefix.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### *async* wait_exit()

Block until the process exits; return its exit code.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`int`](https://docs.python.org/3/builtins/functions.html#int)]

#### *async* wait_healthy()

Poll until the process is accepting TCP connections or timeout.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### *async* enlace.supervise.supervise_all(processes)

Spawn and supervise all managed processes until shutdown.

Registers SIGTERM/SIGINT handlers for graceful shutdown.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)
