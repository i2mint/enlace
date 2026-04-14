# Audit: Backend Generality and Process Supervision Readiness

**Date:** 2026-04-14
**Scope:** `enlace/base.py`, `discover.py`, `compose.py`, `serve.py`, `util.py`, `__main__.py`, `diagnose.py`
**Purpose:** Identify every assumption that ties enlace to "Python ASGI app" and assess what it would take to support WSGI apps, arbitrary command processes, and external services.

---

## Current State

enlace today is a **single-process, in-process ASGI compositor**. The pipeline is:

1. **Config** (`base.py`): Load `platform.toml`, produce `PlatformConfig` with a list of `AppConfig` objects.
2. **Discovery** (`discover.py`): Walk `apps/` directories, find Python entry files (`server.py`, `app.py`, `main.py`), import them to detect whether they expose an ASGI callable or typed functions.
3. **Composition** (`compose.py`): Create a single `FastAPI` parent, `mount()` each sub-app as an ASGI callable, apply CORS middleware on the parent, cascade lifespan events.
4. **Serving** (`serve.py`): Spawn one Uvicorn subprocess pointing at `enlace.compose:create_app --factory`.

Every app runs inside one Python process. There is no mechanism for spawning per-app processes, proxying to external services, or handling non-Python backends.

---

## ASGI Assumptions Inventory

### Config Layer (`base.py`)

| # | Location | Assumption | Generalization difficulty |
|---|----------|-----------|--------------------------|
| C1 | `AppConfig.app_type` (line 42) | Type is `Literal["asgi_app", "functions", "frontend_only"]`. No variants for WSGI, process, or external. | **Trivial** -- add literals, handle downstream. |
| C2 | `ConventionsConfig.entry_points` (line 22-25) | Defaults to `["server.py", "app.py", "main.py"]` -- Python files only. | **Trivial** -- but semantics change for non-Python; a `Dockerfile` or `package.json` is a different kind of entry point. |
| C3 | `ConventionsConfig.app_attr` (line 27-29) | Assumes a Python module attribute name (`"app"`). Meaningless for process/external backends. | **Trivial** -- make optional (only relevant for `asgi_app`/`wsgi_app`). |
| C4 | `AppConfig.entry_module_path` (line 41) | Named and typed as a Python module path (`Optional[Path]`). For a process backend, this should be a command string; for external, a `host:port`. | **Moderate** -- need a union or separate fields per mode (`command`, `upstream_url`). |
| C5 | `PlatformConfig.backend_port` (line 73) | Single port for the entire backend. Assumes all apps share one Uvicorn process. Process-mode apps each need their own port. | **Moderate** -- need per-app port allocation or a port range. |
| C6 | `AppConfig` has no `command`, `port`, `host`, `health_check_path`, `env`, or `ready_timeout` fields. | These are all needed for process/external modes. | **Moderate** -- add optional fields, validate per `app_type`. |

### Discovery Layer (`discover.py`)

| # | Location | Assumption | Generalization difficulty |
|---|----------|-----------|--------------------------|
| D1 | `_find_entry_point` (lines 112-118) | Searches for files named `server.py`, `app.py`, `main.py`. Non-Python apps don't match. | **Moderate** -- could extend `entry_points` to include `Dockerfile`, `package.json`, `go.mod`, or a generic `app.toml` type declaration. But the semantics diverge: finding a file vs. interpreting what kind of app it is. |
| D2 | `_detect_app_type` (lines 127-157) | **Imports the module** via `importlib.import_module` to inspect it. Checks for an `app` attribute (ASGI callable) or public functions. This is the deepest coupling to Python. | **Requires rearchitecture** -- for non-Python backends, type detection must come from config (`app.toml`), not runtime introspection. |
| D3 | `_import_module_from_path` (lines 211-241) | Manipulates `sys.path`, builds dotted module names from filesystem paths, calls `importlib.import_module`. Pure Python machinery. | N/A for non-Python -- this function wouldn't be called. |
| D4 | `discover_apps` (lines 244-286) | Calls `_detect_app_type` for every app, which imports the module. This means **all Python apps must be importable at discovery time** -- their dependencies must be installed. | **Significant** for process-mode Python apps too. You may want to discover apps without importing them (e.g., read `app.toml` declarations instead). |
| D5 | `ConventionDiscoverer` protocol (line 27-28) | `discover(apps_dir) -> list[AppConfig]` -- the interface itself is fine. The coupling is in the implementation, not the contract. | **Trivial** -- the protocol is backend-agnostic. |

### Composition Layer (`compose.py`)

| # | Location | Assumption | Generalization difficulty |
|---|----------|-----------|--------------------------|
| O1 | `build_backend` (lines 25-105) | Creates a single `FastAPI()` parent and `mount()`s all sub-apps on it. This is the **fundamental architectural assumption**: every backend is an ASGI callable that can run in-process. | **Requires rearchitecture** -- process/external backends can't be mounted; they need reverse-proxy routes (Caddy config generation) or httpx-based proxy middleware. |
| O2 | `_load_sub_app` (lines 108-138) | Imports the module via `importlib`, gets `app` attribute via `getattr`. Python-only. | N/A for non-Python. |
| O3 | `_build_router_from_functions` (lines 141-154) | Wraps Python functions as FastAPI POST/GET routes. Python-specific, but intentionally so (this is a distinct app type). | No change needed -- this is a Python-specific feature. |
| O4 | `cascade_lifespan` (lines 54-68) | Propagates startup/shutdown to mounted sub-apps by iterating `Mount` routes. Only relevant for in-process ASGI apps. | N/A for process/external -- they manage their own lifecycle. |
| O5 | `CORSMiddleware` on parent (lines 75-82) | Applied to the single FastAPI parent. For process/external backends, CORS must be handled by each process individually or by the reverse proxy (Caddy). | **Moderate** -- for mixed mode (some mounted, some proxied), CORS config must be split. |
| O6 | `ENLACE_MANAGED=1` (lines 41-43) | Set as an env var before mounting. Process-mode apps would inherit this if spawned by enlace, but external services wouldn't. | **Trivial** -- pass it in the subprocess env. |
| O7 | `create_app` factory (lines 173-180) | Returns a single `FastAPI` -- designed for `uvicorn --factory`. In a multi-process world, this factory would only compose the in-process-mounted apps, not all apps. | **Moderate** -- needs to become aware of which apps are mounted vs. proxied. |

### Serving Layer (`serve.py`)

| # | Location | Assumption | Generalization difficulty |
|---|----------|-----------|--------------------------|
| S1 | `serve` function (lines 43-124) | Constructs a single `uvicorn` command for `enlace.compose:create_app`. Only one child process is ever spawned. | **Requires rearchitecture** -- needs to spawn N child processes (one per process-mode app) plus optionally one Uvicorn for in-process-mounted apps. |
| S2 | `_children` list (line 16) | Infrastructure for multiple children exists, but only one is ever appended (line 118). | **Trivial** -- the data structure is already a list. |
| S3 | `_graceful_shutdown` (lines 20-40) | Forwards signals to all children and waits. Already handles multiple children correctly. | **Ready** -- works as-is for multi-process. |
| S4 | No health checking | No mechanism to verify a child is ready to accept connections before considering it "up." | **Missing infrastructure** (see gaps section below). |
| S5 | No restart logic | If the Uvicorn child dies, `serve` exits. No supervisor behavior. | **Missing infrastructure**. |
| S6 | No log capture or routing | Child stdout/stderr are passed through to the parent's stdout/stderr (line 117). No per-app log separation. | **Missing infrastructure** for multi-process. |
| S7 | No port allocation | No mechanism to assign or track ports for per-app processes. | **Missing infrastructure**. |
| S8 | Reload is Uvicorn-only (lines 106-110) | `--reload` and `--reload-dir` are Uvicorn flags. Non-Python processes would need their own file-watching or none at all. | **Moderate** -- could use `watchfiles` directly for process restart on change. |

### Diagnostic Layer (`diagnose.py`)

| # | Location | Assumption | Generalization difficulty |
|---|----------|-----------|--------------------------|
| G1 | All scanners | Diagnose checks for Python imports (`import fastapi`), Python middleware patterns (`BaseHTTPMiddleware`), Python-style hardcoded URLs. Would not detect issues in a Node.js or Go app. | **Moderate** -- would need language-specific scanners or a generic pattern approach. Not urgent; diagnosis can be scoped to Python apps. |

---

## Generalization Assessment

### Backend Type 1: Python WSGI App (Flask, Django)

**What changes per layer:**

- **Config**: Add `"wsgi_app"` to `AppConfig.app_type`. Trivial.
- **Discovery**: `_detect_app_type` could check for a WSGI-like callable (two-arg `(environ, start_response)` pattern) or check for known frameworks (Flask, Django). Moderate -- but fragile. Better: let `app.toml` declare `type = "wsgi"`.
- **Composition (in-process mounting)**: Use `a]2wsgi` or `WSGIMiddleware` from Starlette (`starlette.middleware.wsgi.WSGIMiddleware`) to wrap the WSGI app as an ASGI callable, then mount normally. **This is a well-trodden path** -- Starlette has built-in support. Low effort.
- **Composition (process mode)**: Spawn the WSGI app via `gunicorn` or `waitress` on its own port, proxy to it. Same as any process-mode app.
- **Serving**: No change for in-process. For process mode, see Type 2 below.
- **Diagnostics**: Minor additions to recognize Flask/Django patterns.

**Verdict:** Easiest of the three to support. In-process mounting via `WSGIMiddleware` requires ~10 lines of change in `compose.py`. Process mode falls into the general "arbitrary command" case.

### Backend Type 2: Arbitrary Command on a Port (`node server.js`, `go run main.go`, `uvicorn some:app --port 8003`)

**What changes per layer:**

- **Config**: Add `"process"` to `app_type`. Add fields: `command: Optional[str]`, `port: Optional[int]`, `health_check_path: Optional[str] = "/health"`, `ready_timeout: int = 30`, `env: dict[str, str] = {}`. Moderate.
- **Discovery**: Skip `importlib` for process-type apps. Type must be declared in `app.toml` (`type = "process"`, `command = "node server.js"`, `port = 8003`). The discoverer reads TOML; no Python introspection. Moderate refactor of `_discover_app` to branch before import.
- **Composition**: Two options:
  - **(a) Reverse-proxy route in Caddy**: enlace generates a Caddyfile stanza that routes `/api/{app_name}/*` to `localhost:{app_port}`. No change to `compose.py`; routing is external. **Preferred for production.**
  - **(b) In-process httpx proxy**: Add an ASGI middleware that proxies requests to the child process. Useful for dev (keeps everything in one `localhost:8000`). Moderate -- ~50 lines, but adds latency and error-mode complexity.
- **Serving**: **This is the big change.** `serve.py` must become a process supervisor:
  - Spawn each process-mode app as a child.
  - Wait for health check to pass before considering it ready.
  - Optionally restart on crash (with backoff).
  - Forward signals to all children on shutdown (already partially implemented).
  - Allocate ports (from config or auto-assign from a range).
- **Diagnostics**: New category -- can the command be found? Does the port conflict with another app?

**Verdict:** Moderate-to-significant effort. The config and discovery changes are straightforward. The real work is in `serve.py` (process supervision) and in choosing the routing strategy (Caddy generation vs. in-process proxy).

### Backend Type 3: External Service at a Known `host:port`

**What changes per layer:**

- **Config**: Add `"external"` to `app_type`. Add `upstream_url: Optional[str]` (e.g., `"http://192.168.1.50:3000"`). Trivial.
- **Discovery**: Declared via `app.toml` only (`type = "external"`, `upstream = "http://..."`). No filesystem entry point needed. The discoverer just reads the TOML. Trivial.
- **Composition**: Same two options as Type 2 (Caddy route or in-process proxy), but simpler -- no process to spawn. The proxy middleware just forwards to the known upstream.
- **Serving**: No process to manage. Just ensure the routing (Caddy config or proxy middleware) is in place. Trivial.
- **Diagnostics**: New check -- is the upstream reachable? Does it respond to health checks?

**Verdict:** Easiest to add once process-mode infrastructure exists. Config + discovery changes are trivial. Routing is the same reverse-proxy or in-process-proxy pattern as Type 2.

---

## Recommended App Modes

A taxonomy of three modes, with `mount` being the current behavior and the other two being new:

### `mount` -- In-process ASGI mounting

- **What it is:** The current behavior. The Python ASGI (or WSGI-wrapped) callable is imported and mounted on the parent FastAPI app via `app.mount()`.
- **Discovery:** Import the module, find the `app` attribute. Current code works.
- **Lifecycle:** Managed by the parent process. Lifespan events are cascaded. No separate process.
- **Routing:** Handled by Starlette's mount dispatch. All apps share one port.
- **When to use:** Lightweight Python ASGI/WSGI apps during local development. Convenient (one process, one port, shared middleware). Not suitable for apps with heavy dependencies, incompatible event loops, or non-Python backends.
- **Config:** `type = "mount"` (or auto-detected as today). Fields: `entry_point`, `app_attr`.

### `process` -- Managed child process

- **What it is:** enlace spawns the app as a child process. The app listens on its own port. enlace manages its lifecycle (start, health-check, restart, shutdown).
- **Discovery:** Type and command declared in `app.toml`. No module import.
- **Lifecycle:** enlace spawns the process, polls a health endpoint until ready, restarts on crash (with exponential backoff), forwards SIGTERM on shutdown.
- **Routing:** Caddy routes to the app's port (production). In dev, enlace can optionally run a proxy middleware on the parent FastAPI to keep everything on one port.
- **When to use:** Any app that runs as its own server -- Python (`uvicorn myapp:app --port 8003`), Node (`node server.js`), Go (`./myapp`), or anything else. Also useful for Python apps with heavy dependencies you don't want in the parent process.
- **Config:** `type = "process"`. Fields: `command`, `port`, `health_check_path`, `ready_timeout`, `env`, `restart_policy`.

### `external` -- Pre-existing service

- **What it is:** The app is already running somewhere (another machine, a Docker container, a cloud service). enlace doesn't manage its lifecycle -- just routes to it.
- **Discovery:** Declared in `app.toml` with an upstream URL.
- **Lifecycle:** None. enlace assumes it's running. Optional health monitoring (log warnings if unreachable, but don't crash).
- **Routing:** Same as `process` -- Caddy route or proxy middleware.
- **When to use:** Databases with admin UIs, services running on other machines, Docker containers managed by `docker-compose`, third-party SaaS with a local endpoint.
- **Config:** `type = "external"`. Fields: `upstream_url`, `health_check_path` (optional).

### Interaction with `functions` and `frontend_only`

The existing `functions` type is a variant of `mount` -- it auto-generates a FastAPI sub-app from Python functions. No change needed; it remains a mount-mode specialization.

`frontend_only` is orthogonal to backend mode -- an app can have a frontend and a `process`-mode backend, or a frontend with no backend at all.

---

## Current Gaps for Process Supervision

If `process` mode were implemented today, these are the missing pieces in `serve.py`:

### 1. Multi-process spawning

**Current state:** `serve()` spawns exactly one Uvicorn child.

**Needed:** Iterate over process-mode `AppConfig` objects, spawn each as a child process. The existing `_children` list and `_graceful_shutdown` already support multiple children -- the gap is only in the spawning logic.

### 2. Port allocation

**Current state:** One `backend_port` on `PlatformConfig`.

**Needed:** Either:
- (a) Each process-mode app declares its port in `app.toml` (simplest, user manages conflicts).
- (b) enlace auto-allocates from a port range (e.g., `process_port_range = [8010, 8099]`), and exposes the assigned port via an env var (`ENLACE_PORT`) so the app knows where to listen.

Option (a) is simpler and sufficient for a personal platform with <20 apps.

### 3. Health checking / readiness

**Current state:** No health checks. `serve()` calls `proc.wait()` and blocks.

**Needed:**
- After spawning a child, poll `http://localhost:{port}{health_check_path}` until it returns 2xx or the `ready_timeout` expires.
- Log readiness status per app.
- Don't generate Caddy routes for apps that aren't ready (or route to a "starting up" page).

A simple polling loop with `httpx` or `urllib` is sufficient. No need for a health-check framework.

### 4. Restart on crash

**Current state:** If the child dies, `serve()` exits (`proc.wait()` returns, function ends).

**Needed:**
- A supervisor loop: `while not _shutting_down: proc.wait(); if exit_code != 0: restart_with_backoff()`.
- Exponential backoff (e.g., 1s, 2s, 4s, 8s, max 60s) to avoid restart storms.
- Max restart count before giving up (configurable, default ~5).
- Log each restart with the exit code and backoff delay.

This is the most significant new infrastructure. It's ~50-80 lines of straightforward code, but it changes the control flow of `serve()` from "launch and wait" to "launch, supervise, react."

### 5. Log capture and routing

**Current state:** `stdout=sys.stdout, stderr=sys.stderr` -- all output is interleaved.

**Needed for multi-process:**
- Prefix each log line with the app name, or
- Route each child's output to a separate log file, or
- Use structured logging (JSON lines) with an `app` field.

For a personal platform, prefixing with `[app_name]` is sufficient. More sophisticated approaches (log files, structured logging) can wait.

### 6. Caddy config generation

**Current state:** No Caddy integration in code (the spec mentions it, but it's not implemented).

**Needed for process mode in production:**
- Generate `reverse_proxy` stanzas for each process/external app.
- Reload Caddy after config changes (`caddy reload`).
- This is mentioned in the spec as a Phase 4 item (`deploy.py`).

For dev, an in-process proxy middleware avoids the Caddy dependency. For production, Caddy config generation is the right path.

### 7. Environment variable passing

**Current state:** `ENLACE_MANAGED=1` is set in `os.environ` (inherited by all children).

**Needed:** Per-app env vars from `app.toml`, plus `ENLACE_PORT` for auto-allocated ports. Pass via `subprocess.Popen(env=...)`.

---

## If In-Process Mounting Were an Optional Plugin

The task description asks: *if we made in-process mounting an optional plugin rather than the default composition strategy, what would simplify?*

### What simplifies

1. **Discovery doesn't need to import modules.** The deepest coupling (D2, D3, D4) goes away. All app type information comes from `app.toml` declarations or simple file-presence heuristics (has `server.py` -> probably Python, has `package.json` -> probably Node). No `importlib`, no `sys.path` manipulation, no risk of import side effects at discovery time.

2. **`compose.py` becomes optional.** The entire module -- `build_backend`, `_load_sub_app`, `cascade_lifespan`, the parent FastAPI with CORS -- is only needed when at least one app is mount-mode. If no apps are mounted, enlace is purely a process supervisor + config generator.

3. **Shared failure domain disappears.** Today, one sub-app's import error or unhandled exception crashes the entire platform. With process isolation, each app fails independently.

4. **CORS is per-app, not centralized.** Each process handles its own CORS (or Caddy handles it). No need for the "CORS on parent only" rule and the `ENLACE_MANAGED` workaround.

5. **Lifespan cascading is unnecessary.** The `cascade_lifespan` workaround for Starlette's missing feature goes away. Each process manages its own startup/shutdown.

6. **The dependency on FastAPI/Starlette becomes optional.** enlace's core would only need `pydantic` (for config) and `subprocess` (for process management). FastAPI becomes a dependency of the mount plugin, not of enlace itself.

### What gets harder

1. **Local dev requires more ports.** Instead of `localhost:8000/api/todo` and `localhost:8000/api/notes`, you get `localhost:8010` and `localhost:8011`. Either run Caddy locally (adds a dependency) or build an in-process proxy (re-introduces some of the complexity you removed).

2. **"Zero config" story weakens.** Today, dropping a `server.py` into `apps/foo/` just works -- no `app.toml` needed. With process-as-default, you'd need at minimum a `command` declaration. The auto-detection ("it has an `app` attribute, so mount it") is a mount-mode feature.

3. **Hot reload needs a different mechanism.** Uvicorn's `--reload` watches files and restarts the process. For process-mode, enlace would need its own file-watcher (e.g., `watchfiles`) to restart children on change.

### Recommendation

Make **process** the default mode for production and new apps. Keep **mount** as a convenience for local development of lightweight Python ASGI apps. The default path should be:

```
app.toml declares type + command + port
    -> enlace spawns the process
    -> Caddy (or dev proxy) routes to it
```

Mount mode is opted into explicitly (or auto-detected for backward compatibility when a Python entry point exists and no `app.toml` is present). This preserves the current "zero config" experience for simple Python apps while making the system ready for heterogeneous backends.
