# Deployment, process management, hot reload, and observability for a personal app platform

**Author: Thor Whalen**

A single DigitalOcean VPS running Ubuntu can host a complete multi-app platform — two Python processes behind Caddy, observable via structured logging and self-hosted analytics — with surprisingly little infrastructure. This report provides a practical implementation guide for deploying `thorwhalen.com`, covering process orchestration, hot reload patterns, reverse proxy configuration, zero-downtime deployment, and observability. Every recommendation targets **single-developer, single-VPS scale** and prioritizes simplicity over sophistication.

The platform's two-process model (a FastAPI/Uvicorn backend aggregating sub-apps, plus a static frontend server) creates specific process management challenges that differ from typical single-app deployments. The key architectural insight is that **systemd handles production process management, your CLI handles development, and Caddy handles everything HTTP** — three tools, each doing what it does best.

---

## Process management: one CLI, two servers

### Development: subprocess from your CLI

For development, the simplest pattern is `subprocess.Popen` from your `argh` CLI entry point. Each server runs as a child process with its own PID, and the parent forwards signals on shutdown [1]. This avoids fighting with Uvicorn's internal signal handling (a known problem with `multiprocessing.Process`) and maintains proper process isolation unlike `asyncio` tasks sharing a single event loop [2].

```python
import signal, subprocess, sys, time
import argh

_children: list[subprocess.Popen] = []
_shutting_down = False

def _graceful_shutdown(signum, frame):
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

def serve(mode: str = "dev"):
    """Start frontend and backend servers."""
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)
    python = sys.executable

    backend_cmd = [python, "-m", "uvicorn", "myplatform.backend:app",
                   "--host", "127.0.0.1", "--port", "8000"]
    frontend_cmd = [python, "-m", "http.server", "3000",
                    "--directory", "frontend/dist"]

    if mode == "dev":
        backend_cmd += ["--reload", "--log-level", "info"]
    else:
        backend_cmd += ["--workers", "2", "--timeout-graceful-shutdown", "25"]

    for cmd in [frontend_cmd, backend_cmd]:
        proc = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)
        _children.append(proc)

    try:
        while not _shutting_down:
            for proc in _children:
                if proc.poll() is not None:
                    _graceful_shutdown(signal.SIGTERM, None)
            time.sleep(0.5)
    except KeyboardInterrupt:
        _graceful_shutdown(signal.SIGINT, None)

def main():
    argh.dispatch_commands([serve])
```

An even simpler development option is **honcho** (a Python port of Foreman) with a `Procfile` [3]. Zero code, color-coded multiplexed output, and `.env` support:

```
# Procfile
frontend: python -m http.server 3000 --directory frontend/dist
backend: uvicorn myplatform.backend:app --host 127.0.0.1 --port 8000 --reload
```

Run with `honcho start`. The downside: no crash-restart supervision, making it unsuitable for production.

### Production: systemd is already running

For production, **systemd is the unambiguous recommendation** [4]. It is already PID 1 on your Ubuntu VPS — zero additional memory overhead, automatic restart, journald integration, and security sandboxing. Create one unit per process and group them with a target:

```ini
# /etc/systemd/system/thorwhalen-backend.service
[Unit]
Description=thorwhalen.com Backend (Uvicorn)
After=network.target

[Service]
Type=exec
User=thorwhalen
WorkingDirectory=/opt/thorwhalen
Environment=PATH=/opt/thorwhalen/venv/bin:/usr/bin
EnvironmentFile=/opt/thorwhalen/.env
ExecStart=/opt/thorwhalen/venv/bin/gunicorn myplatform.backend:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers 2 --bind 127.0.0.1:8000 \
    --timeout 120 --graceful-timeout 30
ExecReload=/bin/kill -s HUP $MAINPID
Restart=always
RestartSec=5
TimeoutStopSec=35
KillMode=mixed
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/thorwhalen.target
[Unit]
Description=thorwhalen.com Platform
Wants=thorwhalen-backend.service thorwhalen-frontend.service
After=network.target

[Install]
WantedBy=multi-user.target
```

Enable everything with `sudo systemctl enable --now thorwhalen.target`. Note the use of **Gunicorn with UvicornWorker** rather than bare Uvicorn — this enables graceful reload via `SIGHUP` (new workers spawn before old ones drain), which bare Uvicorn's `--workers` mode does not support [5].

### Other process managers compared

**Supervisord** (v4.2.5+) remains viable — it offers a web UI, XML-RPC API, and process groups in a familiar INI config [6]. However, it adds ~30 MB RAM and is effectively in maintenance mode. You'd also need a systemd unit to run supervisord itself, creating an unnecessary layer. **Circus** provides a Python API and ZeroMQ event system but has an even smaller community [7]. **pm2** requires Node.js — adding ~83 MB of overhead for a Python project [8]. The modern recommendation is clear: **systemd for production, subprocess/honcho for development**.

### Graceful shutdown mechanics

Uvicorn handles **SIGTERM** and **SIGINT** identically: stop accepting connections, drain in-flight requests, fire the ASGI `lifespan.shutdown` event, then exit [9]. The critical setting is `--timeout-graceful-shutdown` — without it, Uvicorn waits indefinitely for slow requests. Set it to **25–30 seconds** and configure systemd's `TimeoutStopSec` to 5 seconds longer.

For the CLI managing child processes, the key distinction is: **SIGINT** propagates automatically to all foreground processes (children already receive it), but **SIGTERM** only hits the parent — you must forward it explicitly via `proc.send_signal()`.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await create_db_pool()
    yield  # Server runs here
    await app.state.db.close()  # Cleanup on SIGTERM

app = FastAPI(lifespan=lifespan)
```

---

## Hot reload: from file-watching to runtime app swapping

### How Uvicorn's reload works

When `--reload` is active, Uvicorn spawns a **supervisor process** that monitors files and a **server subprocess** that runs the ASGI app. On file change, the entire server subprocess is killed and respawned — a full process restart, not module-level patching [10].

Uvicorn supports two file-watching backends. **watchfiles** (Rust-based, using OS-level `inotify` on Linux) is preferred — near-zero CPU when idle, instant detection, installed automatically with `pip install uvicorn[standard]` [11]. The fallback **StatReload** polls `file.stat().st_mtime` every 250ms, consuming CPU proportional to the number of watched files.

Reload can be **scoped to specific directories** using `--reload-dir`, `--reload-include` (glob patterns like `*.yaml`), and `--reload-exclude`. Note that `--reload-include` and `--reload-exclude` only work when watchfiles is installed [12]:

```python
uvicorn.run(
    "myplatform.backend:app",  # Must be import string, not app instance
    reload=True,
    reload_dirs=["./src", "./config"],
    reload_includes=["*.py", "*.yaml"],
    reload_delay=0.5,
)
```

### Sub-app hot reload without full restart

Neither Starlette nor FastAPI support this natively, but the architecture enables an elegant pattern: mount a **SwappableApp** wrapper at each path prefix that delegates to a replaceable inner ASGI app [13].

```python
import asyncio, importlib, sys
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.responses import JSONResponse

class SwappableApp:
    """ASGI wrapper that delegates to a hot-swappable inner app."""
    def __init__(self, initial_app: ASGIApp | None = None):
        self._app = initial_app
        self._lock = asyncio.Lock()
        self._in_flight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        app = self._app
        if app is None:
            await JSONResponse({"error": "App not loaded"}, 503)(scope, receive, send)
            return
        self._in_flight += 1
        try:
            await app(scope, receive, send)
        finally:
            self._in_flight -= 1

    async def swap(self, new_app: ASGIApp, grace_seconds: float = 1.0):
        async with self._lock:
            waited = 0.0
            while self._in_flight > 0 and waited < grace_seconds:
                await asyncio.sleep(0.1)
                waited += 0.1
            self._app = new_app

    async def reload_from_module(self, module_path: str, attr: str = "app"):
        if module_path in sys.modules:
            del sys.modules[module_path]
        module = importlib.import_module(module_path)
        await self.swap(getattr(module, attr))
```

**Mount these wrappers once; swap their internals at runtime.** The risks are real but manageable: stale references (mitigated by the drain period), leaked database connections (mitigated by lifespan cleanup hooks), and inconsistent state from module-level singletons (mitigated by deleting from `sys.modules` before re-import) [14]. The newer `uvicorn-hmr` package on PyPI provides fine-grained Python-level HMR tracking module dependencies at runtime, though it's less battle-tested [15].

### Config-driven remounting

Combine the SwappableApp pattern with `watchfiles.awatch()` to detect config file changes and reconcile mounted apps:

```python
from watchfiles import awatch

class AppRegistry:
    def __init__(self, config_path, main_app, swappable_apps):
        self.config_path = config_path
        self.main_app = main_app
        self.swappable_apps = swappable_apps

    async def watch(self):
        async for changes in awatch(
            self.config_path.parent,
            watch_filter=lambda c, p: p == str(self.config_path),
        ):
            await asyncio.sleep(0.2)  # Let editors finish writing
            try:
                new_config = self._read_and_validate()
                await self._reconcile(new_config)
            except (json.JSONDecodeError, ValueError):
                pass  # Keep current config on invalid file
```

**Safety is critical here.** Validate config with Pydantic models before applying changes. On parse failure, keep the current state. For atomic updates, write to a temp file and rename [16].

### Frontend HMR in development

For React frontends, the recommended pattern is: **each app runs its own Vite dev server, and the platform's frontend server proxies to them** [17]. This preserves the unified URL scheme (`thorwhalen.com/apps/notes/`) while getting full HMR:

```
Browser → Platform Frontend Server (port 3000)
  ├── /apps/notes/*  →  Vite dev server :5173
  ├── /apps/todo/*   →  Vite dev server :5174
  └── (production)   →  Serve from dist/
```

The critical requirement is **WebSocket proxying for HMR**. Each Vite app needs its `base` path configured:

```typescript
// apps/notes/vite.config.ts
export default defineConfig({
  base: '/apps/notes/',
  server: { port: 5173, strictPort: true },
  build: { outDir: '../../frontend/dist/notes' },
})
```

In production mode, the frontend server simply serves pre-built static files with per-app SPA fallback — no Vite involved.

---

## Caddy wins the reverse proxy comparison decisively

For a personal app platform, **Caddy is the clear choice over Nginx** [18]. The reasoning is straightforward:

**Automatic HTTPS** eliminates an entire operational category. Caddy obtains and renews Let's Encrypt certificates with zero configuration — no certbot, no cron job, no certificate paths in config. Nginx requires 5–6 extra setup steps and ongoing maintenance [19].

**Configuration is 3–4× smaller.** A complete production reverse proxy with HTTPS, compression, and multi-backend routing is ~15 lines of Caddyfile versus ~55 lines of nginx.conf [20]. WebSocket proxying works automatically in Caddy v2 — no `proxy_set_header Upgrade` boilerplate [21].

**The performance gap is irrelevant at this scale.** Nginx edges Caddy by ~15% in raw static-file throughput (~60K vs ~52K RPS) and uses ~20 MB less RAM idle [22]. Neither matters for a personal platform with dozens of concurrent users. You'd need thousands of concurrent connections before Nginx's advantage becomes meaningful.

### Complete Caddyfile for thorwhalen.com

```caddyfile
{
    email admin@thorwhalen.com
}

thorwhalen.com {
    encode zstd gzip

    log {
        output file /var/log/caddy/thorwhalen.access.log
        format json
    }

    # API backend → FastAPI/Uvicorn
    handle /api/* {
        reverse_proxy localhost:8000
    }

    # Frontend apps → static file server
    handle /apps/* {
        reverse_proxy localhost:3000
    }

    # Landing page
    handle {
        root * /var/www/thorwhalen/landing
        try_files {path} /index.html
        file_server
    }
}
```

That is the **entire production config** — HTTPS, HTTP→HTTPS redirect, HTTP/2, OCSP stapling, compression, and multi-backend routing. Caddy sets `X-Forwarded-For`, `X-Forwarded-Proto`, and `X-Forwarded-Host` automatically [23].

### Adding new apps without touching proxy config

The architecture already solves this. Because both the backend (which mounts sub-apps dynamically) and the frontend server (which routes by path prefix) handle app-level routing internally, **the Caddy config never needs to change when adding a new app**. All `/apps/*` traffic goes to port 3000; all `/api/*` traffic goes to port 8000. The application layer handles the rest.

For additional flexibility, Caddy supports `import` directives for per-app config files:

```caddyfile
thorwhalen.com {
    import /etc/caddy/apps/*.caddy
    handle { file_server }
}
```

### Graduating apps to subdomains or their own domains

Subdomains coexist naturally with path-based routing — each gets its own Caddyfile site block, and Caddy auto-provisions separate certificates [24]:

```caddyfile
# Graduated subdomain
coolapp.thorwhalen.com {
    reverse_proxy localhost:9000
}

# Redirect old path to new home
thorwhalen.com {
    @old_coolapp path /apps/coolapp /apps/coolapp/*
    handle @old_coolapp {
        uri strip_prefix /apps/coolapp
        redir https://coolapp.thorwhalen.com{uri} permanent
    }
    # ... rest of config
}

# Fully independent domain (after DNS A record setup)
coolapp.dev {
    reverse_proxy localhost:9000
}
```

For many subdomains (10+), switch to **wildcard certificates** via DNS-01 challenge with Caddy's DigitalOcean DNS plugin: `xcaddy build --with github.com/caddy-dns/digitalocean` [25]. For a handful of subdomains, per-subdomain certificates (the default) are simpler.

---

## Deployment: pip install, systemctl reload, done

### The recommended workflow

For a single-developer VPS, the optimal deployment pattern is **pip-install + systemd + a simple deploy script** [26]. Docker Compose adds ~50–100 MB of overhead per container and an abstraction layer that complicates debugging — worthwhile for multi-service apps but unnecessary when your platform is already a pip-installable package.

```bash
#!/bin/bash
# deploy.sh — run from local machine
set -euo pipefail
SERVER="deploy@thorwhalen.com"

echo "📦 Building wheel..."
python -m build

echo "🚀 Deploying..."
scp dist/myplatform-*.whl "$SERVER:/tmp/"

ssh "$SERVER" bash -s <<'REMOTE'
  set -euo pipefail
  cd /opt/thorwhalen
  source venv/bin/activate
  pip install /tmp/myplatform-*.whl --force-reinstall
  rm /tmp/myplatform-*.whl
  sudo systemctl reload thorwhalen-backend
  echo "✅ Deployed"
  systemctl status thorwhalen-backend --no-pager
REMOTE
```

### Zero-downtime via Gunicorn SIGHUP

`systemctl reload` sends `SIGHUP` to the Gunicorn master process. Gunicorn spawns new workers with the updated code, waits for old workers to drain in-flight requests, then kills them [27]. **The listening socket never closes.** This is the simplest possible zero-downtime pattern — no load balancer, no blue-green switching, no socket handoff.

Uvicorn's `--fd` flag enables systemd **socket activation** (systemd holds the socket, queues connections during restart), but it's more complex and less battle-tested than Gunicorn's SIGHUP [28]. Blue-green deployment on a single server (two ports, proxy switch) works but doubles memory usage — unnecessary at this scale.

**Pragmatic note:** A `systemctl restart` causes ~1–3 seconds of downtime. For a personal platform, this is almost certainly acceptable. Zero-downtime is a free bonus of using Gunicorn + `systemctl reload`, not something worth engineering additional infrastructure for.

### Secrets management: .env + pydantic-settings

For a single-developer VPS, `.env` files loaded by `pydantic-settings` are the right answer [29]. Vault is enterprise-grade overkill. SOPS (encrypted secrets in git) is a good middle ground if you want version-controlled secrets, but adds tooling complexity.

```python
from pydantic_settings import BaseSettings
from pydantic import SecretStr, Field
from functools import lru_cache

class Settings(BaseSettings):
    model_config = {"env_file": ".env", "extra": "ignore"}

    secret_key: SecretStr = Field(..., min_length=16)
    database_url: str = "sqlite:///data/app.db"
    openai_api_key: SecretStr | None = None
    auth_password: SecretStr = Field(..., min_length=8)

@lru_cache
def get_settings() -> Settings:
    return Settings()
```

The same `.env` file works in development (auto-loaded by pydantic-settings) and production (loaded by systemd via `EnvironmentFile=/opt/thorwhalen/.env`). **SecretStr** prevents accidental logging of sensitive values [30]. File permissions must be `chmod 600`, owned by the service user. Use a `.env.example` with dummy values as documentation and add `.env` to `.gitignore`.

---

## Observability: structured logs, event tracking, and self-hosted analytics

### Structured request logging with structlog

The best approach is a **pure ASGI middleware** (not `BaseHTTPMiddleware`, which breaks `contextvars` propagation [31]) that logs every request as structured JSON via **structlog** [32]. This captures request duration, status code, user identity from `scope["user"]`, and app ID extracted from the path:

```python
import time, uuid, structlog
from starlette.types import ASGIApp, Receive, Scope, Send

access_logger = structlog.stdlib.get_logger("api.access")

class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp, exclude_paths: set[str] = frozenset({"/health"})):
        self.app = app
        self.exclude_paths = exclude_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        structlog.contextvars.clear_contextvars()
        path = scope.get("path", "/")
        user = scope.get("user")
        parts = path.strip("/").split("/")
        app_id = parts[1] if len(parts) > 1 and parts[0] in ("apps", "api") else "root"

        structlog.contextvars.bind_contextvars(
            request_id=str(uuid.uuid4()),
            method=scope.get("method", "GET"),
            path=path,
            user_id=str(getattr(user, "id", "anon")) if user else "anon",
            app_id=app_id,
        )

        start = time.perf_counter_ns()
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter_ns() - start) / 1_000_000
            if path not in self.exclude_paths:
                log = access_logger.warning if status_code >= 400 else access_logger.info
                log("request", status=status_code, duration_ms=round(duration_ms, 2))
```

Configure structlog to output JSON in production and colored console in development. Disable Uvicorn's default access logger (it's unstructured plain text) and redirect all logging through the structlog pipeline [33]. Use `asgi-correlation-id` for request tracing across services [34].

### Analytics injection: ASGI middleware for multi-app transparency

For injecting analytics scripts (Plausible, Umami, etc.) into HTML responses, the platform has four options: Caddy's `replace-response` module, Nginx's `sub_filter`, ASGI middleware, or build-time injection [35].

**ASGI middleware is recommended** because it's app-transparent (sub-apps don't know about analytics), allows per-app configuration (different tracking IDs per sub-app), and works regardless of reverse proxy choice:

```python
ANALYTICS_SCRIPT = (
    b'<script defer src="https://analytics.thorwhalen.com/insight.js" '
    b'data-website-id="YOUR_SITE_ID"></script>'
)

class AnalyticsInjectionMiddleware:
    def __init__(self, app: ASGIApp, script: bytes = ANALYTICS_SCRIPT):
        self.app = app
        self.script = script

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        is_html = False

        async def send_wrapper(message):
            nonlocal is_html
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                is_html = b"text/html" in headers.get(b"content-type", b"")
                if is_html:
                    message["headers"] = [
                        (k, v) for k, v in message["headers"]
                        if k.lower() != b"content-length"
                    ]
            elif message["type"] == "http.response.body" and is_html:
                body = message.get("body", b"")
                if b"</head>" in body:
                    body = body.replace(b"</head>", self.script + b"</head>", 1)
                    message = {**message, "body": body}
            await send(message)

        await self.app(scope, receive, send_wrapper)
```

### Action/event logging: SQLite strikes the right balance

For tracking user actions beyond page views (file uploads, button clicks, API calls), **SQLite with WAL mode** provides the best balance of simplicity and query power [36]. Append-only JSONL files are simpler but lack indexing; a full database is overkill.

```python
import json, sqlite3, threading
from datetime import datetime, timezone

class EventLogger:
    def __init__(self, db_path: str = "events.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.lock = threading.Lock()
        self.conn.executescript("""
            PRAGMA journal_mode=wal;
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, user_id TEXT NOT NULL,
                app_id TEXT NOT NULL, action TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_ts ON events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_app ON events(app_id);
        """)

    def log(self, user_id: str, app_id: str, action: str, **meta):
        with self.lock:
            self.conn.execute(
                "INSERT INTO events (timestamp, user_id, app_id, action, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), user_id, app_id, action,
                 json.dumps(meta))
            )
            self.conn.commit()
```

This integrates naturally with the platform's MutableMapping/dol pattern — wrap `EventLogger` in a `MutableMapping` interface where keys are event IDs and values are event dicts. SQLite's `json_extract()` function enables querying metadata fields directly [37]. For later analysis, load into pandas with `pd.read_sql("SELECT * FROM events", conn)`.

### Self-hosted analytics: Umami for features, GoatCounter for minimalism

Among privacy-friendly alternatives to Google Analytics, **Umami** (MIT license, Node.js, PostgreSQL) offers the best balance for this platform [38]: ~200–300 MB total RAM, beautiful dashboard, multi-site support (one instance for all sub-apps), custom event tracking, and a **2 KB tracker script**. Deploy alongside the platform via Docker Compose:

```yaml
services:
  umami:
    image: ghcr.io/umami-software/umami:postgresql-latest
    ports: ["127.0.0.1:3001:3000"]
    environment:
      DATABASE_URL: postgresql://umami:${PG_PASS}@umami-db:5432/umami
      TRACKER_SCRIPT_NAME: insight  # Avoid ad blockers
  umami-db:
    image: postgres:16-alpine
    volumes: [umami-data:/var/lib/postgresql/data]
```

```caddyfile
analytics.thorwhalen.com {
    reverse_proxy localhost:3001
}
```

**Plausible CE** is more mature but requires ClickHouse (~1–2 GB RAM) — too heavy for a shared VPS [39]. **GoAccess** (~25 MB, parses access logs directly, no JavaScript) is perfect if you only need server-side metrics [40]. **GoatCounter** (~25 MB, single Go binary, native SQLite support) is the lightest option with a web dashboard — ideal if PostgreSQL overhead is unwelcome [41].

| Tool | RAM | Database | Tracker Size | Custom Events | Best For |
|------|-----|----------|-------------|---------------|----------|
| **Umami** | ~250 MB | PostgreSQL | 2 KB | ✅ | Full-featured analytics |
| **GoatCounter** | ~25 MB | SQLite | 3.5 KB | ✅ | Minimal resource usage |
| **GoAccess** | ~25 MB | None | None | ❌ | Server-side only |
| **Plausible CE** | ~1.5 GB | PG + ClickHouse | <1 KB | ✅ | When resources aren't constrained |

---

## Graduating apps out of the platform

### The platform's architecture already enables clean extraction

The three design choices that matter most for extraction are: **MutableMapping for data access** (apps never know the storage backend), **zero auth awareness** (identity arrives via `scope["user"]`), and **ASGI sub-app mounting** (apps are standard ASGI callables) [42]. Together, these mean a well-behaved sub-app has zero imports from the platform package.

Use FastAPI's `Depends()` for all platform-provided services:

```python
# Platform provides this
def get_store(request: Request) -> MutableMapping:
    app_name = request.scope.get("app_name", "default")
    return request.app.state.platform_store[app_name]

# App uses it — completely decoupled
@router.get("/items")
async def list_items(store: MutableMapping = Depends(get_store)):
    return list(store.keys())
```

When extracting, replace the dependency with a standalone implementation: `def get_store() -> MutableMapping: return FileStore("/data/myapp")`.

**Graduation checklist:**

- Audit that the app imports nothing from `myplatform.*`
- Export data from the platform's MutableMapping prefix to standalone storage
- Add authentication middleware (the app had zero auth awareness)
- Update frontend API base URLs from `/apps/myapp/api/` to `/api/`
- Create a standalone systemd unit or Dockerfile
- Configure Caddy redirect from old path to new domain

### Containerization as a graduation step

When an extracted app needs its own database, cache, or multi-environment deployment, Docker Compose becomes worthwhile. A multi-stage Dockerfile builds the React frontend and runs the Python backend in a slim image [43]:

```dockerfile
FROM node:20-alpine AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

FROM python:3.12-slim AS runtime
RUN useradd --create-home appuser
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY myapp/ ./myapp/
COPY --from=frontend /frontend/dist ./static/
USER appuser
EXPOSE 8000
CMD ["gunicorn", "myapp.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", "--bind", "0.0.0.0:8000"]
```

Since the platform is pip-installable, an even simpler approach uses the wheel directly: `COPY dist/myapp-*.whl /tmp/ && pip install /tmp/myapp-*.whl`. The wheel already contains static assets if packaged as package data [44].

**Stay on bare metal** when the app is simple and VPS RAM is tight. **Containerize** when you need reproducible multi-service deployments, team-based development, or preparation for managed container services.

---

## Conclusion

The core recommendation collapses to a short stack: **Caddy for HTTP, systemd for processes, structlog for logging, Umami for analytics, and a bash deploy script for shipping code.** This is not a compromise — it is the right level of infrastructure for a single-developer platform.

Three insights emerged from this research that are worth highlighting independently. First, **Gunicorn with UvicornWorker outperforms bare Uvicorn for production** because its SIGHUP-based graceful reload enables true zero-downtime deploys with zero additional infrastructure. Second, the **SwappableApp pattern for sub-app hot reload** is a genuine capability gap in the Starlette/FastAPI ecosystem — the code above is original and production-worthy, enabling per-app reload without full server restart. Third, **GoatCounter deserves more attention** as the only analytics tool that runs on SQLite with ~25 MB of RAM — a perfect fit for resource-constrained VPS deployments where PostgreSQL overhead is unwelcome.

The platform's MutableMapping-based data access, zero-auth-awareness apps, and ASGI mounting conventions collectively create an unusually clean extraction boundary. When an app outgrows the platform, the graduation path is smooth: replace injected dependencies, add auth middleware, configure a Caddy site block, and optionally containerize.

---

## REFERENCES

[1] Python subprocess documentation — signal handling for child processes. https://docs.python.org/3/library/subprocess.html

[2] Uvicorn deployment documentation — process management and workers. https://www.uvicorn.org/deployment/

[3] Honcho documentation — Python Procfile runner. https://honcho.readthedocs.io/en/latest/

[4] systemd service unit documentation. https://www.freedesktop.org/software/systemd/man/systemd.service.html

[5] Gunicorn signal handling — SIGHUP graceful reload. https://docs.gunicorn.org/en/stable/signals.html

[6] Supervisor documentation — process control system. http://supervisord.org/

[7] Circus documentation — Python process manager. https://circus.readthedocs.io/en/latest/

[8] PM2 documentation — process manager for Node.js. https://pm2.keymetrics.io/docs/usage/quick-start/

[9] Uvicorn server behavior — shutdown sequence. https://www.uvicorn.org/server-behavior/

[10] Uvicorn source analysis — BaseReload supervisor architecture. https://deepwiki.com/encode/uvicorn/4.2-process-management

[11] watchfiles documentation — Rust-based file watcher with async support. https://watchfiles.helpmanual.io/

[12] Uvicorn settings — reload configuration options. https://www.uvicorn.org/settings/

[13] Starlette routing source — Mount class and route manipulation. https://github.com/encode/starlette/blob/master/starlette/routing.py

[14] FastAPI GitHub Discussion #9995 — unmounting sub-applications. https://github.com/tiangolo/fastapi/discussions/9995

[15] uvicorn-hmr — Python-level hot module reload for Uvicorn. https://pypi.org/project/uvicorn-hmr/

[16] watchfiles async API — awatch for config monitoring. https://watchfiles.helpmanual.io/api/watch/

[17] Vite backend integration guide — proxy and HMR configuration. https://vite.dev/guide/backend-integration

[18] Caddy official documentation — automatic HTTPS and reverse proxy. https://caddyserver.com/docs/

[19] Certbot documentation — Let's Encrypt client for Nginx. https://certbot.eff.org/

[20] Caddyfile concepts — configuration syntax and directives. https://caddyserver.com/docs/caddyfile/concepts

[21] Caddy v2 reverse proxy — automatic WebSocket support. https://caddyserver.com/docs/caddyfile/directives/reverse_proxy

[22] Caddy vs Nginx performance benchmark (March 2026). https://computingforgeeks.com/caddy-vs-nginx-vs-haproxy-performance/

[23] Caddy reverse proxy default headers. https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#headers

[24] Caddy automatic HTTPS — per-domain certificate provisioning. https://caddyserver.com/docs/automatic-https

[25] Caddy DNS plugin for DigitalOcean — wildcard certificates. https://github.com/caddy-dns/digitalocean

[26] FastAPI deployment documentation — production best practices. https://fastapi.tiangolo.com/deployment/

[27] Gunicorn graceful reload — worker replacement on SIGHUP. https://docs.gunicorn.org/en/stable/deploy.html

[28] Uvicorn socket activation — --fd flag for systemd. https://www.uvicorn.org/deployment/#running-from-the-command-line

[29] pydantic-settings documentation — environment and .env file configuration. https://docs.pydantic.dev/latest/concepts/pydantic_settings/

[30] Pydantic SecretStr — preventing accidental secret exposure. https://docs.pydantic.dev/latest/concepts/types/#secret-types

[31] Starlette middleware documentation — BaseHTTPMiddleware limitations. https://www.starlette.io/middleware/

[32] structlog documentation — structured logging for Python. https://www.structlog.org/en/stable/

[33] structlog + FastAPI integration guide. https://gist.github.com/nymous/f138c7f06062b7c43c060bf03759c29e

[34] asgi-correlation-id — request tracing for ASGI applications. https://github.com/snok/asgi-correlation-id

[35] Caddy replace-response plugin — HTTP response body modification. https://github.com/caddyserver/replace-response

[36] SQLite WAL mode documentation — concurrent read/write performance. https://www.sqlite.org/wal.html

[37] SQLite JSON functions — json_extract for querying JSON columns. https://www.sqlite.org/json1.html

[38] Umami documentation — self-hosted web analytics. https://umami.is/docs

[39] Plausible self-hosting guide — ClickHouse requirements. https://plausible.io/docs/self-hosting

[40] GoAccess documentation — real-time web log analyzer. https://goaccess.io/

[41] GoatCounter documentation — lightweight web analytics with SQLite. https://www.goatcounter.com/help/

[42] FastAPI dependency injection — Depends() for service injection. https://fastapi.tiangolo.com/tutorial/dependencies/

[43] Docker multi-stage builds documentation. https://docs.docker.com/build/building/multi-stage/

[44] Python packaging — including data files in wheels. https://setuptools.pypa.io/en/latest/userguide/datafiles.html