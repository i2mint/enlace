# Enlace Generalization Plan: From ASGI Compositor to Process Orchestrator

**For:** Claude Code working on the `enlace` project  
**From:** Thor Whalen — April 2026  
**Status:** Implementation-ready architectural plan

---

## What this document is

This is a plan for extending enlace from a single-process ASGI compositor into a multi-backend process orchestrator. Read the codebase first, then use this plan to guide your implementation. The plan is opinionated about architecture but flexible about implementation details — use your judgment when you see the actual code.

**Reference documents** (read these when you need deeper context on a specific topic):

- **Deep research report:** `/Users/thorwhalen/Dropbox/py/proj/i/enlace/misc/docs/Process orchestration and multi-backend composition for enlace.md` — covers process supervision patterns (supervisord, PM2, circus, honcho), health checking, log multiplexing, graceful shutdown, Caddy integration, auth unification, WebSocket proxying, registry design, convention-over-configuration, non-Python backends, WSGI mounting, and port allocation. Has concrete code examples for everything.
- **Code audit:** `/Users/thorwhalen/Dropbox/py/proj/i/enlace/misc/docs/audit_backend_generality.md` — itemizes every ASGI assumption in the current codebase, per module, with difficulty ratings. Use this as your checklist.

---

## The big picture

enlace currently works like this:

```
platform.toml → discover Python modules → import them → mount() on one FastAPI → serve via one Uvicorn
```

We want it to work like this:

```
platform.toml → discover apps (Python or not) → classify by mode → 
  ├── mode="asgi": import + mount() on gateway FastAPI (current behavior, preserved)
  ├── mode="process": spawn as child process, supervise lifecycle, route via proxy
  ├── mode="external": just route to a known upstream (no lifecycle management)
  └── mode="static": serve files (or build then serve)
```

The key architectural additions are:

1. **A dev-mode process supervisor** built on `asyncio.create_subprocess_exec` — spawns children, health-checks them, restarts on failure with exponential backoff, streams colored logs to the terminal. This is for `enlace dev` / `enlace serve` — interactive, single-terminal development.
2. **Production deployment via systemd** — instead of building a production-grade supervisor (which is just a worse systemd), enlace **generates systemd unit files** from the app registry. One unit per process-mode app, a target to group them. systemd handles restart policies, logging via journald, cgroup isolation, and socket activation — all battle-tested.
3. **A routing compositor** that generates Caddy config (production) or runs a lightweight dev proxy (development) to unify all backends under one domain.

These coexist with the current in-process mounting. The `mode` field in the app config is the discriminator.

**The critical design split:** Dev mode and production mode use different tools for process management. Don't build one supervisor that tries to serve both. The dev supervisor is ~150 lines of asyncio. The production path is a code generator that emits systemd units + Caddyfile, then delegates to the OS.

---

## Guiding principles

Before you start coding, internalize these constraints:

1. **Don't break the current zero-config experience.** A directory with `server.py` exposing an `app` attribute should still work exactly as it does today, with no `app.toml` required. The new modes are additive.

2. **Sub-apps must have zero knowledge of the platform.** This is an existing constraint — preserve it. Process-mode apps receive identity info via headers (set by Caddy's `forward_auth` or the dev proxy), not by importing enlace.

3. **No new heavy dependencies.** The dev supervisor should be pure `asyncio` + stdlib. Health checks can use `httpx` (already likely in the dependency tree) or `urllib`. No `pyzmq`, no `circus`, no `supervisord` as a library dependency.

4. **Use established tools instead of reinventing them.** Specifically:
   - **systemd** for production process supervision (generate unit files, don't build a production supervisor).
   - **`watchfiles`** for file-watching in dev mode (already in the stack from prior research).
   - **`a2wsgi`** for WSGI-to-ASGI wrapping (preferred over Starlette's built-in `WSGIMiddleware`).
   - **`httpx`** for health check HTTP calls (no framework needed — a short async polling loop is all you need).
   - **Caddy** for production routing, TLS, and auth forwarding (already chosen).

5. **Single-developer, single-VPS scale.** Don't design for clustering, container orchestration, or multi-machine deployment. This is one person running a handful of apps on one server.

6. **Follow Thor's coding conventions.** Progressive disclosure, small functions, underscore-prefix for module-internal helpers, docstrings with doctests, dataclasses for config, `argh` for CLI. See the `python-coding-standards` and `python-package-architecture` skills if you have access to them.

---

## Phase 1: Extend the config layer (`base.py`)

**Goal:** Make `AppConfig` capable of describing all four modes without breaking existing configs.

### What to do

Add a `mode` field to `AppConfig` as the core discriminator:

```python
from typing import Literal, Optional

mode: Literal["asgi", "process", "external", "static"] = "asgi"
```

Add fields that are only relevant for non-asgi modes. Make them optional and validate per-mode:

```python
# For process mode
command: Optional[str] = None           # e.g., "node server.js", "uvicorn myapp:app"
port: Optional[int] = None              # port the process listens on
socket: Optional[str] = None            # alternative: Unix domain socket path
env: dict[str, str] = {}                # per-app environment variables
build: Optional[str] = None             # build command, run before start

# For external mode
upstream_url: Optional[str] = None      # e.g., "http://192.168.1.50:3000"

# For static mode
public_dir: Optional[str] = None        # directory to serve, relative to app path

# Shared across process/external
health_check_path: Optional[str] = "/health"
ready_timeout: int = 30                 # seconds to wait for health check

# Restart policy (process mode only)
restart_policy: Literal["always", "on-failure", "never"] = "on-failure"
max_retries: int = 5
restart_delay_ms: int = 100             # initial backoff delay in ms
```

**Important:** Keep the existing `app_type` field (`"asgi_app"`, `"functions"`, `"frontend_only"`) for now — it describes *what was discovered*, not *how to run it*. The new `mode` field describes *how to run it*. They're orthogonal: an `asgi_app` can run in `mode="asgi"` (mounted) or `mode="process"` (spawned via uvicorn). A `functions` app is always `mode="asgi"` (it gets wrapped into a FastAPI router). Don't conflate them.

### Validation

Add a `model_validator` (or `__post_init__` if using dataclasses) that enforces per-mode requirements:

- `mode="process"` requires `command` (or must be inferrable from conventions).
- `mode="external"` requires `upstream_url`.
- `mode="asgi"` requires `entry_module_path` (the current behavior).
- `mode="static"` requires `public_dir` (or defaults to `"public"` or `"dist"`).

### Platform-level config

Add to `PlatformConfig`:

```python
process_port_start: int = 9001          # first port for auto-allocation
socket_dir: str = "/run/enlace"         # directory for Unix domain sockets
```

---

## Phase 2: Extend discovery (`discover.py`)

**Goal:** Make discovery work for non-Python apps without breaking Python auto-detection.

### The key change

Currently, `_detect_app_type` imports every Python module to inspect it. This is the deepest ASGI coupling. The fix is to **check for an `app.toml` first**, and only fall back to Python introspection if no `app.toml` exists.

The logic should be:

```
For each directory in apps/:
  1. If app.toml exists → read it, construct AppConfig from declared fields
  2. Else if Python entry point exists (server.py, app.py, main.py) → current behavior (import, detect)
  3. Else if package.json exists → infer mode="process", command="npm start", port=3000
  4. Else if go.mod exists → infer mode="process", build="go build -o app .", command="./app", port=8080
  5. Else → skip or warn
```

Steps 3-4 are **convention-over-configuration** heuristics. They're nice to have but not essential for v1. The critical path is steps 1-2: explicit declaration via `app.toml` and backward-compatible Python auto-detection.

### app.toml format

This is a per-app config file that lives in the app directory. It overrides anything auto-detected:

```toml
# apps/blog/app.toml
mode = "process"
command = "npm start"
port = 3000
route = "/blog"

[env]
NODE_ENV = "production"

[healthcheck]
path = "/health"
interval = 30
timeout = 10
retries = 3
```

When `app.toml` declares a `mode`, skip Python introspection entirely for that app.

### Don't import process-mode Python apps

Even for Python apps, if `app.toml` says `mode = "process"`, don't import the module. The command field tells enlace how to run it. This matters because process-mode Python apps may have dependencies not installed in the enlace virtualenv.

---

## Phase 3: Dev-mode process supervision (`supervise.py` — new module)

**Goal:** Build a lightweight, dev-mode-only process supervisor on `asyncio.create_subprocess_exec`.

This supervisor is for `enlace dev` / `enlace serve` — interactive development where you want colored logs in one terminal, Ctrl+C to stop everything, and automatic restarts when things crash. **It is not the production supervision story** — that's systemd (Phase 5b). Keeping this scope narrow means ~150 lines of clean asyncio code with no edge cases around daemonization, PID files, or boot ordering.

Create a new module `supervise.py` (don't bloat `serve.py`). The deep research report (§1-5) has detailed code examples and design rationale. Here's the essential architecture:

### Core abstraction: `ManagedProcess`

A dataclass (or small class) that wraps one child process with its lifecycle state:

```python
@dataclass
class ManagedProcess:
    """A supervised child process with health checking and restart logic."""
    name: str
    config: AppConfig
    proc: Optional[asyncio.subprocess.Process] = None
    state: str = "stopped"  # stopped → starting → running → stopping → exited → fatal
    restart_count: int = 0
    _current_delay: float = 0.1  # seconds, grows exponentially
```

### Key behaviors

**Spawning:** Use `asyncio.create_subprocess_exec` with `start_new_session=True` (critical — enables killing the entire process group on shutdown, prevents orphaned children). Merge stderr into stdout. Pipe stdout for log capture.

**Health checking:** After spawning, poll `http://localhost:{port}{health_check_path}` with retries until it returns 2xx or `ready_timeout` expires. Use `httpx.AsyncClient` or `urllib`. Transition to `running` state on success, `fatal` on timeout.

**Restart with exponential backoff:** When a `running` process exits unexpectedly:
- If `restart_policy == "never"`: transition to `exited`, done.
- If `restart_policy == "on-failure"` and exit code was 0: transition to `exited`, done.
- Otherwise: wait `_current_delay` seconds, multiply delay by 1.5 (cap at 15s), increment `restart_count`. If `restart_count > max_retries`: transition to `fatal`. Otherwise: respawn.
- **Reset delay to initial value after 30 seconds of stable uptime.** This is PM2's best idea — it prevents permanent backoff escalation for processes that crash occasionally but run fine most of the time.

**Graceful shutdown:** Send `SIGTERM`, wait up to `kill_timeout` (default 10s), then `os.killpg(proc.pid, SIGKILL)` for stragglers. The deep research report §4 has a complete implementation.

**Log streaming:** Read from `proc.stdout` line by line, prefix each line with a colored app name and timestamp (honcho style). The deep research report §3 has the async implementation. This is dev-mode only — in production, systemd captures stdout/stderr to journald automatically, so no custom log routing is needed.

### The supervisor loop

The top-level function manages all `ManagedProcess` instances:

```python
async def supervise(apps: list[AppConfig]):
    """Spawn and supervise all process-mode apps."""
    managed = [ManagedProcess(name=app.name, config=app) for app in apps]
    
    # Register signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown_all(managed)))
    
    # Spawn all, respecting dependency order if declared
    await asyncio.gather(*(start_process(m) for m in managed))
    
    # Wait until all processes have exited or shutdown is requested
    await asyncio.gather(*(watch_process(m) for m in managed))
```

`watch_process` is the per-app loop that waits for exit, decides whether to restart, applies backoff, and respawns. This is where the state machine lives.

---

## Phase 4: Refactor `serve.py`

**Goal:** Make `serve()` orchestrate both in-process mounting and supervised processes.

The current `serve()` spawns one Uvicorn child. The new version should:

1. Partition apps by mode: `asgi_apps`, `process_apps`, `external_apps`, `static_apps`.
2. If there are any `asgi` apps: spawn one Uvicorn child for the gateway (current behavior, unchanged).
3. If there are any `process` apps: spawn and supervise each one via the new `supervise.py`.
4. Generate routing config (Caddy or dev proxy) that maps routes to the right backends.
5. Wait for all children, handle signals.

The existing `_children` list and `_graceful_shutdown` already handle multiple children — the gap is only in the spawning logic.

### Dev mode vs. production mode

**Dev mode** (`enlace serve` or `enlace dev`): 
- Process-mode apps are managed by the asyncio supervisor from Phase 3.
- All process-mode apps listen on static ports (auto-allocated from `process_port_start`).
- Optionally: a lightweight ASGI proxy middleware on the gateway that routes `/blog/*` to `localhost:9001`, keeping everything accessible on one port. This is convenient but not essential — developers can also just hit the individual ports.
- Uvicorn `--reload` for the gateway; `watchfiles` for process-mode apps (restart child on file change).

**Production mode** (`enlace deploy` or when `ENLACE_ENV=production`):
- Process-mode apps are managed by **systemd**, not the asyncio supervisor.
- `enlace deploy` generates systemd unit files + a Caddyfile, installs them, and reloads the relevant daemons.
- Process-mode apps listen on Unix domain sockets under `socket_dir`.
- Caddy handles all routing (see Phase 5a).
- Logging is handled by journald (`journalctl -u enlace-blog.service`).
- No file watching, no hot reload, no custom restart logic — systemd does all of this.

For now, focus on dev mode. Production deployment (Phase 5) can come later.

---

## Phase 5a: Routing integration (Caddy config generation)

**Goal:** Generate a Caddyfile from the app registry and reload Caddy.

This is a production concern. For v1, it's fine to defer this and just use static ports in dev. But when you get to it:

Create a `caddy.py` module with a function that takes the platform config and generates a Caddyfile string. The deep research report §6 has the full implementation. The key pattern:

- In-process ASGI apps → `reverse_proxy localhost:8000` (the gateway)
- Process-mode apps → `reverse_proxy unix//run/enlace/{name}.sock`
- External apps → `reverse_proxy {upstream_url}`
- Static apps → `file_server` with `root` pointing to the build output

Auth integration via `forward_auth` should point at the gateway's auth verification endpoint. This ensures identical auth behavior for mounted and proxied apps. See deep research report §7 for the unified auth pattern.

Add a CLI command: `enlace caddy generate` → writes Caddyfile, `enlace caddy reload` → validates and reloads.

---

## Phase 5b: Production deployment (systemd unit generation)

**Goal:** Generate systemd unit files from the app registry so production process management is delegated to the OS.

Your VPS already runs systemd. It handles restart with backoff, logging via journald, cgroup isolation, dependency ordering, and socket activation — all battle-tested over a decade. Building a custom production supervisor would mean reimplementing all of this, badly. Instead, enlace should **generate and install systemd units**, following the pattern honcho uses for exporting to supervisord/upstart/systemd.

### What to generate

Create a `deploy.py` (or `systemd.py`) module. For each process-mode app, generate a unit file:

```ini
# /etc/systemd/system/enlace-blog.service
[Unit]
Description=enlace app: blog
After=network.target
PartOf=enlace.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/enlace/apps/blog
ExecStart=/usr/bin/node server.js
Environment=NODE_ENV=production
Environment=ENLACE_MANAGED=1

# Socket binding
# (if using Unix sockets, the app reads ENLACE_SOCKET)
Environment=ENLACE_SOCKET=/run/enlace/blog.sock

# Restart policy — mirrors the app config
Restart=on-failure
RestartSec=1
StartLimitBurst=5
StartLimitIntervalSec=60

# Graceful shutdown
TimeoutStopSec=10
KillMode=mixed
KillSignal=SIGTERM

# Logging (journald handles it)
StandardOutput=journal
StandardError=journal
SyslogIdentifier=enlace-blog

[Install]
WantedBy=enlace.target
```

Also generate a grouping target:

```ini
# /etc/systemd/system/enlace.target
[Unit]
Description=enlace platform
After=network.target caddy.service

[Install]
WantedBy=multi-user.target
```

### CLI commands

```bash
enlace deploy generate    # writes unit files to a staging directory, prints diff
enlace deploy install     # copies to /etc/systemd/system/, runs systemctl daemon-reload
enlace deploy start       # systemctl start enlace.target
enlace deploy stop        # systemctl stop enlace.target
enlace deploy status      # systemctl status enlace-*.service
enlace deploy logs blog   # journalctl -u enlace-blog.service -f
```

### Mapping config to systemd

| `AppConfig` field | systemd directive |
|---|---|
| `command` | `ExecStart` |
| `env` | `Environment=` lines |
| `restart_policy = "always"` | `Restart=always` |
| `restart_policy = "on-failure"` | `Restart=on-failure` |
| `restart_policy = "never"` | `Restart=no` |
| `max_retries` | `StartLimitBurst` |
| `restart_delay_ms` | `RestartSec` (note: systemd doesn't do exponential backoff natively — use a fixed delay here, which is fine for production) |
| `kill_timeout` (if you add it) | `TimeoutStopSec` |

### What this buys you

- **Zero custom supervision code in production.** systemd manages PID tracking, restart, logging, orphan cleanup.
- **`journalctl` for logs.** `journalctl -u enlace-blog -f` for tailing, `journalctl -u enlace-blog --since "1 hour ago"` for history. No custom log rotation.
- **Boot integration.** `WantedBy=multi-user.target` means your apps start on VPS reboot.
- **cgroup isolation.** Each service gets its own cgroup — one app can't starve another of CPU/memory.
- **Familiar ops interface.** `systemctl restart enlace-blog` is something every sysadmin knows.

### What it costs

- Deployment requires `sudo` (writing to `/etc/systemd/system/`).
- systemd's `RestartSec` is a fixed delay, not exponential backoff. This is fine for production — exponential backoff matters more in dev where you're actively breaking things.
- Changes require `daemon-reload` after editing unit files.

### Interaction with Caddy

`enlace deploy` should generate **both** systemd units and the Caddyfile in a single operation. The typical flow:

```
enlace deploy generate   →  writes units + Caddyfile to staging dir
enlace deploy install    →  copies units, reloads systemd, reloads Caddy
enlace deploy start      →  starts everything
```

---

## Phase 6: Convention-over-configuration detection

**Goal:** Auto-detect app type from directory contents when no `app.toml` exists.

This is a quality-of-life feature. The detection heuristics (from the deep research report §10):

| Marker file | Inferred mode | Default command | Default port |
|-------------|---------------|-----------------|--------------|
| `server.py`/`app.py`/`main.py` with ASGI app | `asgi` | (mounted in-process) | — |
| `server.py`/`app.py`/`main.py` without ASGI app | `process` | `uvicorn {module}:app` | auto |
| `package.json` with `start` script | `process` | `npm start` | 3000 |
| `go.mod` | `process` | `go run .` (dev) / `./app` (prod) | 8080 |
| `Cargo.toml` | `process` | `cargo run` (dev) | 8080 |
| `index.html` only | `static` | — | — |

**Important subtlety for Python detection:** If a Python file contains `from flask import Flask` or `from django` but NOT `uvicorn`/`starlette`/`fastapi` in its dependencies, default to `mode="process"` rather than `mode="asgi"`. WSGI apps should not be imported into the ASGI gateway by default. The deep research report §10 has a `detect_python_mode()` function you can use.

This phase is optional for v1. The explicit `app.toml` path is sufficient.

---

## What NOT to build

- **A production process supervisor.** systemd already does this. Don't reimplement PID tracking, daemonization, boot ordering, cgroup isolation, or log rotation. Generate systemd units instead.
- **Container support.** No Docker, no Dockerfile handling. Out of scope for single-VPS.
- **Dependency DAG with topological ordering.** Simple `depends_on` with health-check gating is enough. No need for a full DAG solver.
- **Cluster mode or multi-worker process management.** Each app runs as one process. If someone needs workers, they configure that in their own app (e.g., `gunicorn --workers 4`).
- **A web dashboard for process status.** CLI is sufficient. `enlace status` can print a table.
- **Custom log aggregation infrastructure.** Prefixed stdout in dev, journald in prod. That's it.
- **Custom file watching.** Use `watchfiles` (already in the stack). Don't reinvent it.

---

## Implementation order

Start with the minimal vertical slice that proves the architecture:

1. **Config extension** (Phase 1) — add `mode` and process-related fields to `AppConfig`. Make sure existing configs still parse correctly.
2. **Discovery branch** (Phase 2) — add `app.toml` reading. When `app.toml` says `mode="process"`, skip Python introspection.
3. **Dev supervisor MVP** (Phase 3) — spawn one process-mode app, stream its logs with a name prefix, handle Ctrl+C gracefully. No health checks yet, no restart yet.
4. **Multi-process serve** (Phase 4) — make `serve()` handle both gateway + supervised processes simultaneously.
5. **Health checks** (Phase 3 cont.) — add HTTP health polling after spawn.
6. **Restart with backoff** (Phase 3 cont.) — add exponential backoff restart logic.
7. **Dev file watching** — use `watchfiles` to restart process-mode apps on file changes.
8. **Test with a real non-Python app** — create a tiny Node.js app (or a Python Flask app running via gunicorn) and verify the full flow.

After this vertical slice works end-to-end, layer on production deployment:

9. **systemd unit generation** (Phase 5b) — `enlace deploy generate` emits unit files + target.
10. **Caddy config generation** (Phase 5a) — `enlace deploy generate` also emits a Caddyfile.
11. **Convention detection** (Phase 6) — auto-detect app type from directory contents.
12. **Static-mode support** — build step + file serving.

---

## Testing strategy

- **Unit tests** for config parsing (verify that old platform.toml files still work, that mode validation catches bad configs).
- **Unit tests** for the supervisor state machine (mock subprocess, verify state transitions, backoff timing, max-retry → fatal).
- **Integration test** with a real subprocess: spawn a tiny HTTP server (Python's `http.server` works), verify health check passes, kill it, verify restart, kill it `max_retries` times, verify `fatal` state.
- **End-to-end test** with `enlace serve` running a mixed config (one mounted ASGI app + one process-mode app).

---

## Summary of key architectural decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Dev supervisor | `asyncio.create_subprocess_exec` | Non-blocking, pipe-aware, signal-capable, no dependencies |
| Production supervisor | **systemd** (generate unit files) | Battle-tested restart, journald logging, cgroup isolation, boot integration — don't reinvent |
| Process isolation | `start_new_session=True` (dev) / systemd cgroups (prod) | Enables clean process tree cleanup |
| Restart policy (dev) | PM2-style exponential backoff (100ms → 1.5x → cap 15s → reset after 30s stable) | Best balance of fast recovery and storm prevention |
| Restart policy (prod) | systemd `Restart=on-failure` with fixed `RestartSec` | Simple, reliable, no custom code |
| WSGI wrapping | `a2wsgi` | Preferred over Starlette's built-in `WSGIMiddleware` for streaming/error handling |
| File watching (dev) | `watchfiles` | Already in the stack, well-maintained, async-compatible |
| Health checks | `httpx.AsyncClient` polling loop | Lightweight, no framework needed |
| Port strategy (dev) | Static ports from configurable range | Simple, debuggable with curl |
| Port strategy (prod) | Unix domain sockets | No port conflicts, lower latency, filesystem permissions |
| Routing (dev) | Direct port access (optional: gateway proxy) | Simplest possible dev experience |
| Routing (prod) | Caddy with generated Caddyfile | Automatic HTTPS, zero-downtime reload, forward_auth for unified auth |
| Config format | `app.toml` per app directory | Convention-over-configuration: only needed for non-Python or when overriding defaults |
| Log multiplexing (dev) | Colored, name-prefixed interleaved stdout (honcho style) | Proven developer ergonomics |
| Log multiplexing (prod) | journald via systemd | Zero custom code, `journalctl -u enlace-blog -f` |
