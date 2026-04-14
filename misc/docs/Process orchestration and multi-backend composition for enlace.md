# Process orchestration and multi-backend composition for enlace

*Thor Whalen — April 2026*

**enlace can evolve from a single-process ASGI compositor into a full process orchestrator by layering three capabilities: an `asyncio.subprocess`-based supervisor with PM2-style exponential backoff, a Caddy reverse proxy managed via Caddyfile generation and reload, and a `platform.toml` registry extended with a `mode` field that discriminates in-process ASGI mounts from supervised processes.** The two modes coexist cleanly because they share a single auth service (called by Starlette middleware internally and by Caddy's `forward_auth` externally) and a single routing surface (Caddy). This design keeps the single-developer, single-VPS constraint front and center — no containers, no orchestrators, just processes and a reverse proxy.

The sections below answer each design question with concrete code, tradeoff analysis, and references to mature implementations worth borrowing from.

---

## 1. Process supervision: five tools, one pattern

Every process supervisor implements the same core loop: fork a child, watch for its death (via `SIGCHLD` or polling), decide whether to restart it, and expose the state machine to an operator. The differences lie in configuration ergonomics, restart intelligence, and embeddability.

**supervisord** [1] is the incumbent. Its `[program:x]` INI sections define processes with `autorestart=unexpected` (restart only on non-zero exit), `startsecs=10` (minimum uptime before a process is considered "started"), and `startretries=3`. It tracks process state through a clean state machine: `STOPPED → STARTING → RUNNING → STOPPING → EXITED/FATAL`. Dependency ordering is primitive — an integer `priority` field (lower starts first), not a DAG. The critical option for enlace is **`killasgroup=true`**, which sends `SIGKILL` to the entire process group rather than just the leader, preventing orphaned children when Python apps spawn subprocesses. Supervisord's weakness is that it is not embeddable — it runs as a standalone daemon with an XML-RPC control interface [2].

**PM2** [3] contributes the best restart policy in the field: **exponential backoff** (`exp_backoff_restart_delay: 100`), which starts at 100ms, grows by 1.5x per failure, caps at 15 seconds, and resets to zero after 30 seconds of stable uptime. This prevents thundering-herd restarts without manual tuning. PM2 also introduced `stop_exit_codes: [0]` — a cleaner version of supervisord's `autorestart=unexpected` — and memory-ceiling restarts via `max_memory_restart: '1G'`. Its log management (automatic per-app files under `~/.pm2/logs/`, plus `pm2 logs` for interleaved streaming) is the gold standard for developer experience [4].

**honcho** [5], the Python port of Foreman, offers no restart policies or health checks — but its architecture is directly reusable. Each process wraps `subprocess.Popen` with dedicated reader threads that push `Message` events onto a `multiprocessing.Queue`. The `Manager.loop()` pulls from the queue and feeds a `Printer` that formats output with ANSI-colored, name-prefixed lines (`HH:MM:SS appname | log line`). This queue-based event architecture is the cleanest pattern for enlace's dev-mode log multiplexing. Honcho also exports Procfile definitions to supervisord, systemd, and upstart configs — a pattern enlace should steal for production deployment [6].

**circus** [7] from Mozilla is the closest architectural precedent for what enlace needs. Its programmatic API is embeddable:

```python
from circus import get_arbiter

arbiter = get_arbiter([
    {"cmd": "python api.py", "numprocesses": 1},
    {"cmd": "node chat.js", "numprocesses": 1},
])
arbiter.start()
```

Circus introduces **watchers** (named process groups), an **arbiter** (top-level coordinator), pluggable **stream classes** for log handling (`FileStream`, `TimedRotatingFileStream`), **flapping detection** (automatic disabling of processes that restart too frequently), and **lifecycle hooks** (`before_start`, `after_stop`, etc.). The downside is its ZeroMQ dependency (`pyzmq`), which adds weight for a single-VPS use case. After years of dormancy, circus released v0.19.0 in February 2025, indicating renewed maintenance [8].

**s6-overlay** [9] contributes two ideas worth borrowing: **directory-based service definitions** (one directory per service, containing `run` and `finish` scripts) and **explicit dependency graphs** via a `dependencies.d/` directory with touch-files naming prerequisites. Its two-type model — `oneshot` (init scripts) vs. `longrun` (daemons) — maps cleanly to enlace's distinction between build steps and running servers.

The transferable synthesis for enlace: build on `asyncio.subprocess` as the foundation (non-blocking, pipe-aware, signal-capable), borrow supervisord's state machine and `killasgroup` semantics, implement PM2's exponential backoff as the default restart policy, use honcho's queue-based log multiplexing for dev mode, and adopt circus's embeddable arbiter pattern — all without external dependencies beyond the Python standard library.

---

## 2. Health checking: what to probe and when to restart

Health checking strategies form a spectrum from cheap-but-shallow to expensive-but-meaningful:

| Approach | Used by | What it catches | What it misses |
|----------|---------|----------------|----------------|
| PID liveness | supervisord (native) | Process crash | Deadlocks, hung event loops |
| TCP port probe | Docker HEALTHCHECK, load balancers | Port binding failure | Application-layer errors |
| HTTP endpoint | Kubernetes, supervisor_checks [10] | App-level readiness, DB connectivity | Nothing (if well-designed) |
| Stdout heartbeat | Custom implementations | Silent hangs | Requires custom parsing |
| Memory/CPU threshold | PM2, superlance [11] | Resource leaks | May kill healthy processes under load |

For enlace, **HTTP health endpoints are the right default** because every supervised app is a web server. The implementation should follow Kubernetes conventions: a `GET /healthz` that returns `200` when ready, `503` when draining. The supervisor polls this endpoint on a configurable interval, with retries and a start-up grace period.

Restart policies across mature supervisors converge on three modes with four parameters:

```toml
[apps.myapp.restart]
policy = "on-failure"    # "always" | "on-failure" | "never"
max_retries = 10         # after this many failures, enter FATAL state
delay = "100ms"          # initial delay; grows exponentially (1.5x, cap 15s)
kill_timeout = "10s"     # SIGTERM → wait this long → SIGKILL
```

The **exponential backoff** pattern from PM2 deserves special attention: start at `delay`, multiply by 1.5 on each failure, cap at 15 seconds, and **reset to zero after 30 seconds of stable uptime** [3]. This single mechanism replaces the need for separate `startsecs`, `startretries`, and flapping-detection features.

For supervisord users who need HTTP health checks, the `supervisor_checks` package [10] provides an event-listener approach:

```ini
[eventlistener:health_check]
command=supervisor_complex_check
    -n myapp_check -g myapp_group
    -c '{"http":{"timeout":15,"port":8090,"url":"/healthz","num_retries":3}}'
events=TICK_60
```

---

## 3. Log multiplexing: dev-mode clarity, production-mode structure

Log management for N supervised processes needs two modes that serve different purposes.

**Dev mode: interleaved, color-coded stdout** (honcho style). This is the highest-value, lowest-cost approach for a single developer watching a terminal. The implementation in pure Python, built on `asyncio` rather than honcho's threading model:

```python
import asyncio, sys, datetime

COLORS = ['\033[36m', '\033[33m', '\033[32m', '\033[35m', '\033[34m']
RESET = '\033[0m'

async def stream_logs(proc, name: str, color: str, width: int):
    """Read lines from a subprocess and print with colored prefix."""
    async for line in proc.stdout:
        ts = datetime.datetime.now().strftime('%H:%M:%S')
        text = line.decode().rstrip('\n')
        sys.stdout.write(f"{ts} {color}{name:<{width}}{RESET} | {text}\n")
        sys.stdout.flush()

async def run_all(apps: dict[str, list[str]]):
    width = max(len(name) for name in apps)
    tasks = []
    for i, (name, cmd) in enumerate(apps.items()):
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        color = COLORS[i % len(COLORS)]
        tasks.append(stream_logs(proc, name, color, width))
    await asyncio.gather(*tasks)
```

This produces output like:

```
14:32:01 api       | INFO:     Uvicorn running on http://0.0.0.0:8001
14:32:01 dashboard | ready - started server on 0.0.0.0:3000
14:32:02 worker    | [2026-04-14 14:32:02] Worker ready
```

**Production mode: per-app log files with rotation.** PM2's automatic `~/.pm2/logs/<app>-out.log` and `<app>-error.log` pattern [4] is the right model. supervisord's `stdout_logfile_maxbytes=50MB` with `stdout_logfile_backups=10` provides rotation without external tooling [2]. For enlace, this means writing to `~/.enlace/logs/<app>.log` with size-based rotation.

**Structured JSON logs to a shared sink** is overkill for the single-VPS case but worth supporting for users who run a log aggregator. The Python `logging` module's `QueueHandler` pattern allows child processes to push `LogRecord` objects to a shared queue, where a listener process writes them in any format — JSON-lines, structured text, or forwarded to an external service [12].

Docker-compose's approach combines both: each container's stdout is stored as JSON by the Docker logging driver (`/var/lib/docker/containers/<id>/<id>-json.log`), while `docker-compose logs -f` merges them with color-coded prefixes [13]. This dual-mode pattern — structured storage plus interleaved display — is exactly what enlace should replicate.

---

## 4. Graceful shutdown: signals, timeouts, and orphans

Graceful shutdown follows a universal three-phase protocol: **signal → drain → kill**. The implementation details matter more than the concept.

**Phase 1: Signal.** Send `SIGTERM` to all children simultaneously. Do not send sequentially — parallel signaling ensures all processes begin draining at the same time, minimizing total shutdown duration.

**Phase 2: Drain.** Wait up to `kill_timeout` (default 10 seconds) for each process to exit. Use `asyncio.wait_for` with a shared deadline:

```python
import asyncio, signal, os

async def shutdown_all(children: list, timeout: float = 10.0):
    # Phase 1: SIGTERM to all
    for proc in children:
        if proc.returncode is None:
            proc.terminate()

    # Phase 2: Wait with shared timeout
    try:
        await asyncio.wait_for(
            asyncio.gather(*(p.wait() for p in children if p.returncode is None)),
            timeout=timeout,
        )
        return
    except asyncio.TimeoutError:
        pass

    # Phase 3: SIGKILL stragglers (entire process group)
    for proc in children:
        if proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            await proc.wait()
```

**The critical detail is `start_new_session=True`.** When creating child processes with `asyncio.create_subprocess_exec`, passing `start_new_session=True` makes the child a process group leader. This means `os.killpg(proc.pid, signal.SIGKILL)` kills the child **and all its descendants** — essential when a Node.js app spawns worker threads or a shell script launches background processes [14].

**Signal handler registration in asyncio** must use `loop.add_signal_handler()`, not `signal.signal()`, because the former is async-safe and can schedule coroutines:

```python
loop = asyncio.get_running_loop()
for sig in (signal.SIGTERM, signal.SIGINT):
    loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(graceful_shutdown(s)))
```

Note: `loop.add_signal_handler()` is Unix-only [15].

**Edge cases worth handling:** (1) Orphaned grandchildren — solved by `start_new_session=True` + `os.killpg`. (2) Processes that ignore `SIGTERM` — the timeout-then-`SIGKILL` escalation handles this. (3) Zombie processes — the asyncio event loop reaps children via `await proc.wait()`, but if the supervisor crashes, zombies accumulate until PID 1 reaps them. (4) Signals sent to the supervisor's own process group — if enlace itself is launched from a terminal, `Ctrl+C` sends `SIGINT` to the entire foreground process group. The supervisor must catch this and forward it cleanly rather than letting children die from the raw signal.

---

## 5. Python libraries: build on asyncio.subprocess, borrow from circus

The landscape of Python process supervision libraries is surprisingly thin. Here is an honest assessment:

**`asyncio.subprocess`** (stdlib, Python 3.4+) is the right foundation [15]. It provides `create_subprocess_exec()` for non-blocking process creation, async stream reading for log multiplexing without threads, `proc.terminate()/kill()/send_signal()` for signal management, `start_new_session=True` for process group isolation, and integration with `loop.add_signal_handler()`. What it lacks — restart policies, health checks, state machines — is precisely the value enlace's supervisor layer adds.

**circus** (v0.19.0, Feb 2025) [7] is the best architectural reference. Its `get_arbiter()` API, pluggable stream handlers, and flapping detection are all directly relevant. However, its `pyzmq` dependency is heavy for a tool that runs on a single machine. The right move is to study circus's architecture and reimplement the relevant patterns on pure `asyncio`.

**supervisord** (v4.3.0) [1] is well-maintained but not embeddable — it's a standalone daemon controlled via XML-RPC. It makes sense as a production deployment target (enlace could export its app registry to `supervisord.conf`, following honcho's export pattern) but not as an embedded library.

**honcho** (v2.0.0) [5] is a pattern reference, not a dependency. Its `Manager` + `Queue` + `Printer` architecture is simple enough to reimplement, and its Procfile parsing + export-to-supervisor pattern is worth adopting.

The `multiprocessing` module is designed for parallel computation (`Pool`, shared memory), not for supervising heterogeneous external commands. Use `subprocess` / `asyncio.subprocess` instead [14].

---

## 6. Caddy as the routing compositor

Caddy v2's architecture makes it an ideal reverse proxy for enlace. Its admin API (default `localhost:2019`) supports both granular JSON mutations and full Caddyfile replacement, with **zero-downtime reloads** — new config starts before old config stops, with automatic rollback on failure [16].

**The recommended approach is Caddyfile generation + reload**, not the JSON API. The pattern:

1. enlace reads its app registry (`platform.toml`)
2. Generates a Caddyfile from the registry
3. Validates with `caddy adapt --validate` [17]
4. Reloads via `caddy reload` or POST to `/load`

```python
class CaddyManager:
    def generate_caddyfile(self, domain: str, apps: list[dict]) -> str:
        lines = [f"{domain} {{", "    encode zstd gzip", ""]

        # forward_auth for all non-public paths
        lines.append("    forward_auth localhost:8001 {")
        lines.append("        uri /auth/verify")
        lines.append("        copy_headers X-User X-User-Id X-User-Role")
        lines.append("    }")
        lines.append("")

        for app in apps:
            if app.get("mode") == "process":
                sock = f"unix//run/enlace/{app['name']}.sock"
                lines.append(f"    handle_path {app['route']}/* {{")
                lines.append(f"        reverse_proxy {sock}")
                lines.append(f"    }}")
            # in-process apps are handled by the gateway
        
        lines.append("    handle {")
        lines.append("        reverse_proxy localhost:8000")  # ASGI gateway
        lines.append("    }")
        lines.append("}")
        return "\n".join(lines)

    def reload(self, caddyfile_path: str) -> bool:
        import subprocess
        # Validate first
        result = subprocess.run(
            ["caddy", "adapt", "--config", caddyfile_path, "--validate"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise ValueError(f"Invalid Caddyfile: {result.stderr}")
        # POST to admin API for zero-downtime reload
        import httpx
        content = open(caddyfile_path, "rb").read()
        resp = httpx.post(
            "http://localhost:2019/load",
            content=content,
            headers={"Content-Type": "text/caddyfile"},
        )
        return resp.status_code == 200
```

For **dynamic add/remove during development** without regenerating the whole file, the JSON API offers granular control. Using the `@id` shortcut, routes can be addressed directly:

```bash
# Add a route
curl -X POST "http://localhost:2019/config/apps/http/servers/srv0/routes" \
  -H "Content-Type: application/json" \
  -d '{"@id":"app-blog","match":[{"path":["/blog/*"]}],
       "handle":[{"handler":"reverse_proxy",
                  "upstreams":[{"dial":"localhost:3000"}]}]}'

# Remove it
curl -X DELETE "http://localhost:2019/id/app-blog"
```

The tradeoff: **Caddyfile generation** buys simplicity, human-readability, and version-controllable config; it costs a full-config reload on every change. **JSON API** buys granular mutations without touching other routes; it costs a more complex client and the risk of config drift from the source-of-truth registry. For enlace's scale (single developer, handful of apps), Caddyfile generation is clearly better, with JSON API as an optional fast-path for `enlace dev` hot-reload [16].

---

## 7. Unified auth across in-process and proxied apps

The architecture requires a single auth service that is called by two clients: Starlette middleware (for in-process ASGI apps) and Caddy's `forward_auth` (for proxied apps). Both clients make the same HTTP request to the same verification endpoint.

**Caddy's `forward_auth`** [18] sends a subrequest to the auth service with `X-Forwarded-Method`, `X-Forwarded-Uri`, and cookie/authorization headers. On a `2xx` response, specified headers are copied to the original request and forwarded to the backend. On non-`2xx`, the auth service's response is returned to the client (typically a 401 or redirect to login).

```caddyfile
forward_auth localhost:8001 {
    uri /auth/verify
    copy_headers X-User X-User-Id X-User-Role
}
```

**The Starlette middleware** calls the same endpoint with the same headers:

```python
class SharedAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/auth/"):
            return await call_next(request)

        async with httpx.AsyncClient() as client:
            resp = await client.get("http://localhost:8001/auth/verify", headers={
                "cookie": request.headers.get("cookie", ""),
                "authorization": request.headers.get("authorization", ""),
                "x-forwarded-method": request.method,
                "x-forwarded-uri": str(request.url.path),
            })

        if resp.status_code != 200:
            return RedirectResponse(f"/auth/login?next={request.url.path}")

        request.state.user = resp.headers.get("x-user", "")
        request.state.user_id = resp.headers.get("x-user-id", "")
        return await call_next(request)
```

**The auth verification endpoint** is a simple Starlette route that validates a JWT or session cookie and returns user metadata in response headers:

```python
async def verify(request):
    token = request.cookies.get("enlace_session")
    if not token:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return JSONResponse({"error": "invalid"}, status_code=401)
    return JSONResponse({"ok": True}, headers={
        "X-User": payload["sub"],
        "X-User-Id": str(payload["user_id"]),
        "X-User-Role": payload.get("role", "user"),
    })
```

This pattern ensures **identical auth behavior** regardless of whether an app is mounted in-process or running as a supervised process behind Caddy. Proxied backends (Node.js, Go, etc.) receive `X-User` and `X-User-Id` headers set by Caddy after `forward_auth` succeeds — they simply read these headers and trust them, since Caddy strips any client-supplied values [18].

The comparison to Traefik's `ForwardAuth` [19] is straightforward: both implement the same pattern, but Caddy's Caddyfile syntax is more concise and better suited to a single-VPS setup without container labels.

---

## 8. WebSocket proxying through Caddy

**WebSocket proxying works automatically in Caddy v2 — no special configuration needed** [20]. Unlike nginx (which requires explicit `proxy_set_header Upgrade` and `Connection "upgrade"` directives), Caddy detects the `Upgrade: websocket` header, forwards the upgrade request to the backend, and transitions to a bidirectional tunnel on `101 Switching Protocols`.

```caddyfile
handle /chat/* {
    reverse_proxy localhost:3000  # WebSocket "just works"
}
```

Three caveats require attention:

**Config reloads close WebSocket connections.** By default, all active WebSocket connections are forcibly closed when Caddy reloads its configuration. The mitigation is `stream_close_delay`, which gives WebSocket clients a grace period before disconnection [21]:

```caddyfile
handle /chat/* {
    reverse_proxy localhost:3000 {
        stream_close_delay 5m    # 5-minute grace on reload
        stream_timeout 24h       # max connection lifetime
        flush_interval -1        # immediate flushing (useful for SSE)
    }
}
```

**Timeout defaults are too short for long-lived connections.** Reports of WebSocket connections dropping at ~10 seconds indicate that explicit `stream_timeout` configuration is necessary for real-time applications [22].

**Differences from in-process WebSocket handling:** In Starlette, WebSocket connections are managed directly by the ASGI server (uvicorn) with zero proxy overhead. Through Caddy, there is an additional network hop (localhost) and the connection lifecycle is tied to Caddy's config — reloads can disconnect clients. The practical difference is negligible for a single-VPS setup, but **client-side reconnection logic is mandatory** for proxied WebSocket apps.

---

## 9. Extending platform.toml for supervised processes

The app registry needs a `mode` field as the core discriminator between in-process ASGI mounts and supervised processes. Drawing from docker-compose's health check and dependency semantics [23], PM2's restart configuration [3], and supervisord's process directives [2], the proposed schema:

```toml
[platform]
name = "my-platform"
domain = "example.com"

# ─── In-process ASGI app ───
[apps.dashboard]
path = "apps/dashboard"
route = "/dashboard"
mode = "asgi"                          # mounted in the gateway process
module = "dashboard.app:app"           # Python import path

# ─── Supervised process (reverse-proxied) ───
[apps.blog]
path = "apps/blog"
route = "/blog"
mode = "process"                       # spawned and supervised
command = "npm start"
port = 3000                            # or socket = "/run/enlace/blog.sock"
working_dir = "apps/blog"
env = { NODE_ENV = "production" }

[apps.blog.healthcheck]
path = "/health"
interval = "30s"
timeout = "10s"
retries = 3
start_period = "15s"

[apps.blog.restart]
policy = "on-failure"
max_retries = 5
delay = "100ms"                        # exponential backoff from here

[apps.blog.depends_on]
api = { condition = "healthy" }

# ─── Go service with build step ───
[apps.search]
path = "apps/search"
route = "/search"
mode = "process"
command = "./search-server"
build = "go build -o search-server ."
port = 8080

[apps.search.healthcheck]
path = "/healthz"
interval = "15s"

[apps.search.restart]
policy = "always"

# ─── Background worker (no HTTP route) ───
[apps.worker]
path = "apps/worker"
mode = "process"
command = "celery -A tasks worker --loglevel=info"

[apps.worker.restart]
policy = "always"

# ─── Static site ───
[apps.docs]
path = "apps/docs"
route = "/docs"
mode = "static"
build = "mkdocs build"
public_dir = "site"
```

The design borrows **`healthcheck`** semantics from docker-compose (interval, timeout, retries, start_period) [23], **restart policy** from PM2's exponential backoff [3], **`depends_on` with conditions** from compose's `service_healthy`/`service_started` [23], and **`build`** from Render's `buildCommand` [24]. The `mode` field (`"asgi"` | `"process"` | `"static"`) is enlace-specific and determines whether the app is mounted in-process, spawned as a child, or served as static files.

---

## 10. Convention-over-configuration: what can be inferred

Modern PaaS platforms (Heroku [25], Railway/Nixpacks [26], Render [24]) demonstrate that **language detection is reliable; framework detection is heuristic; operational config must be explicit**.

Detection logic scans a directory for marker files in priority order:

| Marker | Language | Default build | Default run | Default port |
|--------|----------|--------------|-------------|-------------|
| `Dockerfile` | Container | `docker build` | per `CMD` | per `EXPOSE` |
| `requirements.txt` or `pyproject.toml` | Python | `pip install -r ...` | framework-dependent | 8000 |
| `package.json` | Node.js | `npm install` | `npm start` | 3000 |
| `go.mod` | Go | `go build -o app .` | `./app` | 8080 |
| `Cargo.toml` | Rust | `cargo build --release` | `./target/release/<name>` | 8080 |
| `Gemfile` + `config.ru` | Ruby/Rack | `bundle install` | `bundle exec puma` | 3000 |

For Python specifically, the critical distinction is **ASGI vs. WSGI**. This can be inferred heuristically by scanning dependencies: `fastapi`, `starlette`, `litestar`, or `quart` in `requirements.txt` indicates an ASGI app eligible for in-process mounting; `flask` or `django` (without `uvicorn`/`daphne`) indicates WSGI, which should default to supervised-process mode:

```python
def detect_python_mode(app_dir: Path) -> str:
    deps = (app_dir / "requirements.txt").read_text().lower()
    asgi_frameworks = {"fastapi", "starlette", "litestar", "quart"}
    if any(fw in deps for fw in asgi_frameworks):
        return "asgi"   # eligible for in-process mount
    if "django" in deps and ("uvicorn" in deps or "daphne" in deps):
        return "asgi"   # Django ASGI mode
    return "process"    # default to supervised process
```

**What must always be explicit:** custom ports, environment variables and secrets, health check paths, inter-app dependencies, and the final decision on `mode` when the heuristic is ambiguous (e.g., Django can be either ASGI or WSGI). The convention system should propose defaults that the developer confirms or overrides in `platform.toml`.

**Edge cases to handle:** `package.json` present for asset tooling in a Python project (should not trigger Node.js detection); Django with both `wsgi.py` and `asgi.py` (prefer ASGI if `uvicorn` is in dependencies); monorepos with multiple marker files at different levels.

---

## 11. Non-Python backend integration patterns

For each common backend type, a supervisor needs to know three things: how to start it, how to stop it, and how to verify it is ready.

**Node.js** [27]: Start with `node server.js` (not `npm start`, because npm does not reliably forward signals to the child process). Stop with `SIGTERM`; Node.js apps that call `server.close()` in their signal handler will drain in-flight requests before exiting. Health check via `GET /health`. Node.js natively supports Unix domain sockets: `server.listen('/tmp/app.sock')`.

**Go** [28]: Requires a build step (`go build -o app .`) producing a single static binary with no runtime dependencies. The binary handles `SIGTERM` via `signal.NotifyContext`, and Go's `http.Server.Shutdown()` provides built-in graceful drain. Go servers listen on Unix sockets via `net.Listen("unix", "/tmp/app.sock")`.

**Rust** [29]: Similar to Go (static binary, no runtime dependencies) but with **significantly longer build times** — release builds can take minutes for non-trivial projects. The supervisor should never run `cargo build` on restart; instead, build separately and supervise the binary. Both Axum and Actix-web support graceful shutdown via `tokio::signal`. Axum listens on Unix sockets via `tokio::net::UnixListener`.

**Ruby** [30]: Detect via `Gemfile` + `config.ru`. Start with `bundle exec puma` (Puma auto-discovers `config.ru`). Puma handles `SIGTERM` for graceful shutdown and supports Unix sockets via `-b unix:///tmp/app.sock`. Puma has the richest signal vocabulary: `SIGUSR1` for phased restart, `SIGUSR2` for full restart.

---

## 12. Non-ASGI Python backends: mount or supervise?

Flask and Django WSGI apps can be served two ways, with clear tradeoffs:

**In-process mounting via `WSGIMiddleware`** runs the WSGI app inside the ASGI gateway's event loop using a thread pool. The `a2wsgi` library [31] (recommended over Starlette's deprecated built-in `WSGIMiddleware`) adapts WSGI to ASGI:

```python
from starlette.applications import Starlette
from starlette.routing import Mount
from a2wsgi import WSGIMiddleware
from flask import Flask

flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "Hello from Flask"

gateway = Starlette(routes=[
    Mount("/legacy", app=WSGIMiddleware(flask_app)),
])
```

This buys single-process simplicity and a single port to manage. It costs process isolation — a segfault in a Flask C extension crashes the entire gateway — and limits concurrency to the thread pool size.

**Supervised process with gunicorn** gives full isolation:

```bash
gunicorn --bind unix:/run/enlace/flask-api.sock --workers 2 myflaskapp:app
```

This buys crash isolation, independent scaling, and the ability to use different Python versions or dependency sets. It costs an additional process to manage and a Unix socket or port to allocate.

**Decision rule:** Use in-process mounting for small, trusted WSGI apps that are part of the same codebase (internal tools, admin panels). Use supervised processes for anything with C extensions, heavy dependencies, or that needs independent failure isolation. Default to supervised process mode — it is always safer.

---

## 13. Port allocation: Unix domain sockets win

Three strategies exist, each with distinct tradeoffs for a single-VPS setup:

**Static ports** (e.g., 9001, 9002, 9003...) buy simplicity and easy debugging (`curl localhost:9001`). They cost manual coordination and risk conflicts with other services on the VPS.

**Dynamic port allocation** (find a free port, pass via `$PORT`) eliminates conflicts but introduces a race condition between port discovery and server binding. Python's `socket.bind(('', 0))` finds a free ephemeral port, but another process can claim it before the server starts [32].

**Unix domain sockets** are the best option for production. They eliminate port conflicts entirely, provide **~5-10% lower latency** than TCP loopback (no checksumming, no routing), use filesystem permissions for security, and produce self-documenting paths (`/run/enlace/blog.sock`). The only downside is that they're local-only — irrelevant behind a reverse proxy — and require cleanup of stale `.sock` files after crashes.

Caddy's syntax for Unix socket upstreams [16]:

```caddyfile
handle /blog/* {
    reverse_proxy unix//run/enlace/blog.sock
}
```

Every common web server supports Unix sockets:

```bash
# Python (uvicorn)
uvicorn app:app --uds /run/enlace/api.sock

# Python (gunicorn)
gunicorn --bind unix:/run/enlace/api.sock app:app

# Ruby (puma)
bundle exec puma -b unix:///run/enlace/ruby.sock

# Node.js
server.listen(process.env.SOCKET_PATH || '/tmp/app.sock')
```

**The recommended hybrid for enlace:** Unix domain sockets in production (under `/run/enlace/`), static ports from a configured range (9001+) in development for easy browser and `curl` access. Set `ENLACE_SOCKET` for production and `PORT` for dev; apps check `ENLACE_SOCKET` first:

```python
import os
SOCKET_DIR = "/run/enlace"

def get_bind_address(app_name: str, dev_port: int) -> str:
    if socket_path := os.environ.get("ENLACE_SOCKET"):
        return f"unix:{socket_path}"
    if os.environ.get("ENLACE_ENV") == "production":
        path = f"{SOCKET_DIR}/{app_name}.sock"
        os.makedirs(SOCKET_DIR, exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        return f"unix:{path}"
    return f"0.0.0.0:{os.environ.get('PORT', dev_port)}"
```

---

## Conclusion: the enlace supervisor in 200 lines

The research converges on a clear architecture. **enlace's process supervisor should be built on `asyncio.create_subprocess_exec`** with `start_new_session=True`, implementing a `STOPPED → STARTING → RUNNING → STOPPING → EXITED → FATAL` state machine borrowed from supervisord. The default restart policy should be PM2's exponential backoff (100ms initial, 1.5x growth, 15s cap, 30s stability reset). Health checking should use HTTP endpoint probes with configurable interval, timeout, and retries.

**Caddy serves as the routing compositor**, with enlace generating a Caddyfile from `platform.toml` and reloading via the admin API. Auth is unified through a single verification endpoint called by both Starlette's `SharedAuthMiddleware` (in-process) and Caddy's `forward_auth` (proxied). WebSocket proxying requires no special configuration beyond `stream_close_delay` and `stream_timeout` for long-lived connections.

**Unix domain sockets are the production default** for inter-process communication, eliminating port conflicts and reducing latency. Static ports serve as the development fallback for debugging convenience.

The key insight across all this research is that enlace does not need to become a generic orchestrator. The single-developer, single-VPS constraint is a feature, not a limitation. It means no ZeroMQ (circus's heaviest dependency), no container runtime (s6-overlay's context), no cluster mode (PM2's complexity). What remains is a clean, embeddable supervisor loop that manages a handful of processes, a Caddyfile generator that maps routes to sockets, and a TOML registry that distinguishes `mode = "asgi"` from `mode = "process"`. This is achievable in a few hundred lines of pure-Python asyncio code, with no dependencies beyond the standard library and `httpx` for health checks.

---

## References

[1] Supervisor Project. "Supervisor: A Process Control System." http://supervisord.org/ — GitHub: https://github.com/Supervisor/supervisor

[2] Supervisor Project. "Configuration File — supervisor.conf." http://supervisord.org/configuration.html

[3] PM2. "Application Declaration — ecosystem.config.js." https://pm2.keymetrics.io/docs/usage/application-declaration/

[4] PM2. "Log Management." https://pm2.keymetrics.io/docs/usage/log-management/

[5] Honcho. "Python port of Foreman." https://github.com/nickstenning/honcho — Docs: https://honcho.readthedocs.io/

[6] Honcho. "Exporting to process managers." https://honcho.readthedocs.io/en/latest/export.html

[7] Circus Project. "circus — A Process & Socket Manager." https://circus.readthedocs.io/ — GitHub: https://github.com/circus-tent/circus

[8] Circus. "PyPI Release History." https://pypi.org/project/circus/#history

[9] s6-overlay. "s6-overlay — s6 for containers." https://github.com/just-containers/s6-overlay

[10] supervisor_checks. "Health checks for supervisord." https://github.com/vovanec/supervisor_checks

[11] Superlance. "Supervisor event listener plugins." https://github.com/Supervisor/superlance

[12] Python Documentation. "logging.handlers — QueueHandler and QueueListener." https://docs.python.org/3/library/logging.handlers.html#queuehandler

[13] Docker Documentation. "View container logs (docker compose logs)." https://docs.docker.com/reference/cli/docker/compose/logs/

[14] Python Documentation. "asyncio — Subprocesses." https://docs.python.org/3/library/asyncio-subprocess.html

[15] Python Documentation. "asyncio — Event Loop — add_signal_handler." https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.add_signal_handler

[16] Caddy. "API Documentation." https://caddyserver.com/docs/api

[17] Caddy. "Command Line — caddy adapt." https://caddyserver.com/docs/command-line#caddy-adapt

[18] Caddy. "forward_auth (Caddyfile directive)." https://caddyserver.com/docs/caddyfile/directives/forward_auth

[19] Traefik. "ForwardAuth Middleware." https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/forwardauth/

[20] Caddy. "v2 Upgrade Guide — WebSockets." https://caddyserver.com/docs/v2-upgrade

[21] Caddy GitHub. "Issue #6420 — WebSocket connections closed on config reload." https://github.com/caddyserver/caddy/issues/6420

[22] Caddy GitHub. "Issue #6958 — WebSocket timeout at ~10 seconds." https://github.com/caddyserver/caddy/issues/6958

[23] Docker. "Compose Specification — Services." https://docs.docker.com/reference/compose-file/services/

[24] Render. "Blueprint Specification." https://render.com/docs/blueprint-spec

[25] Heroku. "Buildpacks." https://devcenter.heroku.com/articles/buildpacks

[26] Nixpacks. "Python Provider." https://nixpacks.com/docs/providers/python — GitHub: https://github.com/railwayapp/nixpacks

[27] Node.js Documentation. "Net — server.close()." https://nodejs.org/api/net.html#serverclosecallback

[28] Go Documentation. "net/http — Server.Shutdown." https://pkg.go.dev/net/http#Server.Shutdown

[29] Axum Documentation. "axum::serve — with_graceful_shutdown." https://docs.rs/axum/latest/axum/serve/struct.Serve.html

[30] Puma. "Signals — SIGTERM, SIGUSR1, SIGUSR2." https://github.com/puma/puma/blob/master/docs/signals.md

[31] a2wsgi. "Convert between ASGI and WSGI." https://github.com/abersheeran/a2wsgi

[32] Python Documentation. "socket — Low-level networking interface." https://docs.python.org/3/library/socket.html