# enlace — Specification

**Author:** Thor Whalen  
**Date:** 2026-04-08  
**Status:** Ready for Implementation  
**Package Name:** `enlace`

---

## 1. What enlace Is

**enlace** (Spanish: *link*, *connection*) is a Python CLI tool and library for composing, serving, and deploying multiple web applications from a single codebase. It is a personal app platform — a launch pad for developing apps locally and selectively publishing them to a single domain.

The core idea: drop a Python module or a React app into a directory, and enlace discovers it, mounts it, serves it, and optionally gates it behind auth — with zero boilerplate. Everything enlace infers is inspectable (`enlace show-config`) and overridable (via TOML or CLI flags).

**enlace is not** a framework. Apps don't import it. It wraps, mounts, and routes *to* apps without requiring them to know it exists.

---

## 2. Supporting Research

This specification is informed by six research reports. These documents live in `misc/docs/` and should be consulted during implementation for detailed rationale, code patterns, known pitfalls, and library evaluations:

| Document | Covers |
|---|---|
| `asgi_composition.md` | ASGI `mount()` vs `include_router()`, dynamic mounting, middleware propagation, lifespan cascading, error isolation, performance at 50 mounts, `DynamicDispatcher` pattern |
| `auth_cross_cutting.md` | Pure ASGI auth middleware, path-dispatched access levels, shared-password gates, cookie sessions, CSRF, WebSocket auth, identity injection via ASGI scope, `PrefixedStore` |
| `convention_over_configuration.md` | Convention stack design, layered override hierarchy, meta-configuration, `show-config` discoverability, `ConventionDiscoverer` algorithm, TOML validation with Pydantic |
| `user_data_persistence.md` | `MutableMapping` partitioning, `PrefixedStore`, Mall pattern, per-user store injection middleware, backend swapping (filesystem ↔ S3) |
| `frontend_serving.md` | Path-based SPA routing, Caddy `handle_path` + `try_files`, `SPAStaticFiles` for dev, HTML injection for analytics/nav, `<base href>` for asset paths |
| `deployment_observability.md` | `subprocess.Popen` dev orchestration, systemd units, Caddy reverse proxy, `structlog` logging, Plausible analytics injection, zero-downtime deploys |

When the text below says *"see `asgi_composition.md` §3"*, it means: open that document and find section 3 for the full rationale, working code, and edge cases.

---

## 3. Architecture Overview

enlace runs as **two processes** (or one, configurable):

```
                    ┌──────────────────────────────────┐
                    │           Caddy (prod)           │
                    │    TLS · domain · gzip · HSTS    │
                    └──────┬──────────────┬────────────┘
                           │              │
                    /api/* │              │ /*
                           ▼              ▼
               ┌───────────────┐  ┌──────────────────┐
               │ Backend Server│  │ Frontend Server   │
               │  (Uvicorn)    │  │ (Caddy / Python)  │
               │               │  │                   │
               │ ┌───────────┐ │  │ /apps/todo/       │
               │ │ Auth MW   │ │  │ /apps/chord/      │
               │ │ Store MW  │ │  │ /apps/notes/      │
               │ │ Log MW    │ │  │ /  (launcher)     │
               │ ├───────────┤ │  └──────────────────┘
               │ │ /api/todo │ │
               │ │ /api/chord│ │
               │ │ /api/notes│ │
               │ └───────────┘ │
               └───────────────┘
```

**Backend server:** A single FastAPI/ASGI process. Each app backend is mounted as a sub-application under `/api/{app_name}/`. Cross-cutting concerns (auth, user-store injection, logging, CORS) are applied as pure ASGI middleware on the parent — apps never see them. See `asgi_composition.md` for the full rationale on `mount()` vs `include_router()`, middleware propagation rules, and the `cascade_lifespan` pattern.

**Frontend server:** In production, Caddy serves static assets with per-app SPA fallback. In development, a Python-based static server (`SPAStaticFiles`) handles the same role. See `frontend_serving.md` for Caddy config patterns and the HTML injection approach for analytics/navigation.

**Rationale for separation:** Long-running backend computations don't block asset serving. Independent scaling later. See `deployment_observability.md` for the process orchestration patterns.

---

## 4. Project Structure

Following the `python-package-architecture` conventions:

```
enlace/
├── pyproject.toml
├── README.md
├── CLAUDE.md                       # Agent instructions for working on enlace
├── .claude/                        # Claude skills, rules, commands
├── enlace/
│   ├── __init__.py                 # Public API facade
│   ├── __main__.py                 # CLI entry point (argh dispatch)
│   ├── base.py                     # Core data structures (AppConfig, PlatformConfig)
│   ├── util.py                     # Internal helpers
│   ├── discover.py                 # Convention-based app discovery
│   ├── compose.py                  # ASGI app composition (mount, include_router)
│   ├── serve.py                    # Server orchestration (dev + prod)
│   ├── auth.py                     # Pure ASGI auth middleware
│   ├── stores.py                   # MutableMapping wrappers, PrefixedStore, Mall
│   ├── inject.py                   # HTML injection middleware (analytics, nav)
│   ├── frontend.py                 # SPAStaticFiles, launcher page
│   ├── deploy.py                   # Caddyfile/systemd generation
│   ├── templates/                  # Caddyfile, systemd unit, login page templates
│   ├── misc/
│   │   ├── docs/                   # Research documents (the six reports)
│   │   └── CHANGELOG.md
│   └── tests/
│       ├── test_discover.py
│       ├── test_compose.py
│       ├── test_auth.py
│       └── test_stores.py
└── tests/                          # Integration tests
    └── test_platform.py
```

---

## 5. Convention-over-Configuration System

### 5.1 The Convention Stack

enlace discovers apps by walking a directory (default: `apps/`). Each subdirectory that contains a recognized entry point is treated as an app.

```
apps/
├── chord_analyzer/
│   ├── server.py           # Backend: Python functions or ASGI app
│   ├── frontend/           # Frontend: built React app or static HTML
│   │   └── index.html
│   └── app.toml            # Optional: per-app overrides
├── todo/
│   └── server.py           # Backend only — no frontend
├── my_blog/
│   └── frontend/
│       └── index.html      # Frontend only — no backend
└── platform.toml           # Global configuration + meta-conventions
```

**Default conventions (configurable via `platform.toml [conventions]`):**

| What | Convention | Override |
|---|---|---|
| Route prefix | Directory name → `/api/{name}/` | `route` in `app.toml` |
| Backend entry | First of `server.py`, `app.py`, `main.py` found | `entry_point` in `app.toml` |
| ASGI app object | Attribute named `app` in the entry module | `app_attr` in `app.toml` |
| Frontend assets | `frontend/` subdirectory containing `index.html` | `frontend_dir` in `app.toml` |
| Frontend build | `package.json` present → run `npm run build` | `build_command` in `app.toml` |
| Access level | `local` (development default) | `access` in `app.toml` |
| Display name | Directory name, `_` → space, title-cased | `display_name` in `app.toml` |
| Skip directory | Name starts with `_` or `.` | — |

**Auto-detection of app type:**

When a backend entry module is found, enlace inspects it to determine the app type:

1. **Standalone ASGI app:** Module has an attribute matching `app_attr` (default `app`) that is an ASGI-compatible object (FastAPI, Starlette, or raw ASGI callable). → Mount via `app.mount()`.
2. **Function collection:** Module has no `app` attribute but contains public functions with type annotations. → Wrap with `qh` (or a thin `APIRouter` builder) and mount. See `asgi_composition.md` §2 for the decision framework.
3. **Frontend-only:** No backend entry found, but `frontend/index.html` exists. → Serve static assets only.

### 5.2 Override Hierarchy

Precedence from lowest to highest:

```
hardcoded defaults
  → filesystem conventions
    → app.toml (per-app)
      → platform.toml [apps.{name}] (global per-app overrides)
        → environment variables (ENLACE_APPS__{NAME}__{KEY})
          → CLI flags (--app chord_analyzer --route /custom/)
```

See `convention_over_configuration.md` §2 for the full analysis of layered configuration patterns across ecosystems, and §7 for the concrete synthesis this hierarchy is modeled on.

### 5.3 Configuration Files

**`platform.toml`** — global configuration and meta-conventions:

```toml
[platform]
apps_dir = "apps"                      # Where to discover apps
domain = "thorwhalen.com"              # Production domain
backend_port = 8000
frontend_port = 3000

[conventions]
entry_points = ["server.py", "app.py", "main.py"]  # Ordered priority
app_attr = "app"                       # Attribute name for ASGI object
route_from = "directory_name"          # How route prefixes are derived
frontend_dir = "frontend"             # Subdir name for frontend assets

[auth]
session_secret = "${ENLACE_SESSION_SECRET}"  # env var reference
default_access = "local"               # Default access level for new apps

[auth.shared_passwords]
# Argon2id hashes, never plaintext. Generate with: enlace hash-password
dashboard = "$argon2id$v=19$m=65536,t=3,p=4$..."

[storage]
backend = "filesystem"                 # or "s3", "sqlite"
path = "./data"                        # for filesystem backend

[observability]
log_format = "json"                    # or "console" for dev
analytics_script = ""                  # Plausible/GA snippet to inject
```

**`app.toml`** — per-app overrides (all fields optional):

```toml
route = "/api/my-custom-route/"
entry_point = "application.py"
app_attr = "my_app"
access = "protected:shared"
display_name = "My Custom App"
frontend_dir = "dist"
build_command = "npm run build"
```

### 5.4 Discoverability

Every convention must be inspectable. This is non-negotiable. See `convention_over_configuration.md` §1.3 for the discoverability principles.

```bash
$ enlace show-config
Platform Configuration (resolved)
==================================

Meta-conventions (from platform.toml):
  entry_points: ['server.py', 'app.py', 'main.py']
  route_from: directory_name
  apps_dir: apps

Discovered Apps:
  chord_analyzer
    route:    /api/chord_analyzer/   [convention: directory_name]
    entry:    apps/chord_analyzer/server.py  [convention: first match]
    type:     asgi_app               [detected: has 'app' attribute]
    access:   local                  [default]
    frontend: apps/chord_analyzer/frontend/  [convention: has index.html]

  dashboard
    route:    /api/dash/             [override: app.toml line 2]
    entry:    apps/dashboard/server.py  [convention: first match]
    type:     functions              [detected: no 'app', has typed functions]
    access:   protected:shared       [override: app.toml line 4]
    frontend: None

Conflicts: None
Warnings: None
```

Additional diagnostic commands:

```bash
$ enlace show-config --verbose          # Include provenance for every field
$ enlace show-config --json             # Machine-readable output
$ enlace check                          # Validate config, check for conflicts
$ enlace list-apps                      # Just the app names and routes
```

### 5.5 Discovery Algorithm

The discovery module (`discover.py`) implements the `AppDiscoverer` protocol, modeled on pytest's collector pattern. See `convention_over_configuration.md` §7.5 for the full `ConventionDiscoverer` implementation.

```python
from typing import Protocol
from pathlib import Path

class AppDiscoverer(Protocol):
    """Protocol for app discovery strategies."""
    def discover(self, apps_dir: Path) -> list[AppConfig]: ...
```

The default `ConventionDiscoverer` walks the apps directory, applies naming conventions, loads per-app TOML overrides, and returns a list of validated `AppConfig` objects. The `AppConfig` and `PlatformConfig` models use Pydantic for validation and provenance tracking.

**Conflict detection is fail-fast.** If two apps resolve to the same route prefix, enlace raises immediately with a clear error naming both apps and suggesting fixes. Silent last-writer-wins is an anti-pattern (see `convention_over_configuration.md` §8).

---

## 6. Backend Composition

### 6.1 ASGI App Assembly

The composition module (`compose.py`) builds the aggregate ASGI application from discovered `AppConfig` objects.

```python
def build_backend(config: PlatformConfig) -> FastAPI:
    """Compose all app backends into a single ASGI application.
    
    >>> platform_config = PlatformConfig.from_toml('platform.toml')
    >>> app = build_backend(platform_config)
    """
```

**Decision framework for each app:**

- If the app has an ASGI object (`app_attr` found) → `app.mount(prefix, sub_app)`
- If the app has dispatchable functions (no ASGI object) → build an `APIRouter`, add routes, `app.include_router(router)`
- If the app is frontend-only → skip backend mounting

See `asgi_composition.md` §2 for the full `mount()` vs `include_router()` comparison table and the concrete code pattern.

### 6.2 Middleware Stack

Middleware is applied on the **parent** app only. Order matters — outermost runs first:

```python
app = FastAPI(lifespan=cascade_lifespan)

# 1. Auth — first, nothing bypasses it
app.add_middleware(PlatformAuthMiddleware, auth_config=auth_config, ...)

# 2. User store injection — needs scope["user"] from auth
app.add_middleware(StoreInjectionMiddleware, mall=mall)

# 3. Request logging
app.add_middleware(StructuredLoggingMiddleware)

# 4. CORS
app.add_middleware(CORSMiddleware, allow_origins=config.cors_origins)
```

**Critical rules:**
- All middleware must be **pure ASGI** (the three-callable pattern). Never use `BaseHTTPMiddleware` — it has terminal bugs. See `asgi_composition.md` §4 and `auth_cross_cutting.md` Part I.
- CORS goes on the parent only. If a sub-app also adds CORS, you get duplicate headers.
- Exception handlers on the parent do not propagate to mounted sub-apps. Use `IsolationMiddleware` for untrusted apps. See `asgi_composition.md` §5.

### 6.3 Lifespan Cascading

Starlette does **not** propagate lifespan events to mounted sub-apps. enlace must implement `cascade_lifespan` to forward startup/shutdown. See `asgi_composition.md` §6 for the implementation and `app.state` sharing pattern.

### 6.4 Dynamic Registration (Future)

For hot-reload scenarios, the `DynamicDispatcher` pattern from `asgi_composition.md` §3 provides runtime app registration without mutating `app.routes`. This is a development convenience — not a launch requirement. Cache invalidation (`app.openapi_schema = None`, `app.middleware_stack = None`) is needed when modifying routes at runtime.

---

## 7. Frontend Serving

### 7.1 Development Mode

A Python `SPAStaticFiles` class serves each app's frontend assets with SPA fallback (serve `index.html` for unmatched paths within a prefix). See `frontend_serving.md` for the implementation.

The dev frontend server also serves a **launcher page** at `/` listing all discovered apps with links.

### 7.2 Production Mode

Caddy serves frontend assets directly. enlace generates a Caddyfile from the resolved config:

```bash
$ enlace generate caddyfile > /etc/caddy/Caddyfile
```

The generated Caddyfile uses `handle_path` for per-app SPA fallback and `reverse_proxy` for backend API routing. See `frontend_serving.md` for the Caddy config patterns, and `deployment_observability.md` §"Caddy wins" for why Caddy over Nginx.

### 7.3 Frontend Build Pipeline

If an app's frontend directory contains `package.json`, enlace can run the build command before serving:

```bash
$ enlace build                  # Build all apps with package.json
$ enlace build chord_analyzer   # Build one app
```

Build output directory defaults to `frontend/dist/` (overridable via `build_output_dir` in `app.toml`).

### 7.4 HTML Injection

Platform-level concerns (analytics scripts, navigation bar, auth state) are injected into served HTML via middleware that inserts content before `</head>` or `</body>`. See `frontend_serving.md` for the injection middleware pattern and `deployment_observability.md` §"Observability" for analytics injection specifically.

---

## 8. Authentication and Authorization

Auth is a **cross-cutting concern** — pure ASGI middleware on the parent app. Apps have zero auth awareness. See `auth_cross_cutting.md` for the complete design, security analysis, and code.

### 8.1 Access Levels

| Level | Description | Mechanism |
|---|---|---|
| `local` | localhost only, development | Not served in production |
| `public` | Open to everyone | No auth check |
| `protected:shared` | Single shared password | Login form → signed session cookie |
| `protected:user` | Per-user accounts | Cookie-based sessions (not JWT) |

### 8.2 The Auth Middleware

`PlatformAuthMiddleware` is a pure ASGI middleware that:

1. **Normalizes the path** (collapse `//`, resolve `..`, decode URL encoding) — this prevents the bypass vulnerabilities documented in `auth_cross_cutting.md` Part III.
2. **Resolves the access level** by longest-prefix match against the config registry.
3. **Applies the appropriate auth check** (none, shared-password cookie, user session).
4. **Injects identity** into `scope["state"]["user_id"]` and `scope["state"]["user_email"]` for downstream apps.
5. **Defaults to deny** for unmatched paths.

See `auth_cross_cutting.md` Part I for the full implementation, including the `_normalize_path` function, constant-time password comparison, and `itsdangerous`-signed cookies.

### 8.3 Session Management

- **Shared-password gates:** `itsdangerous.URLSafeTimedSerializer` for signed cookies scoped per-app path.
- **Per-user sessions:** Server-side `SessionStore` backed by a `MutableMapping` (dict for dev, SQLite via `dol` for prod). Only the session ID travels in the cookie.
- **Cookie attributes:** `HttpOnly=True`, `Secure=True` (prod), `SameSite=Lax`.
- **CSRF:** Signed double-submit cookie pattern via `starlette-csrf`. See `auth_cross_cutting.md` Part III.

### 8.4 WebSocket Auth

Browsers send cookies with the WebSocket upgrade handshake. The auth middleware handles `scope["type"] == "websocket"` identically to HTTP. **Origin header validation** is mandatory to prevent Cross-Site WebSocket Hijacking. See `auth_cross_cutting.md` Part III.

### 8.5 Future: OAuth2/OIDC

When "Sign in with Google/GitHub" is needed, add **Authlib** integration as platform-level routes (`/auth/login/google`, `/auth/callback`). Apps remain unaware. See `auth_cross_cutting.md` Part II for the Authlib integration code and the IdP recommendation ladder.

### 8.6 Utility Commands

```bash
$ enlace hash-password             # Interactively hash a shared password (Argon2id)
$ enlace create-session-secret     # Generate a secure session secret
```

---

## 9. User Data Persistence

### 9.1 The `MutableMapping` Abstraction

All data access goes through Python's `MutableMapping` interface. The storage backend is configured at the platform level — apps don't know or care what's behind the interface.

See `user_data_persistence.md` for the full design, including the `PrefixedStore`, `Mall` pattern, backend selection guide, and security considerations (key validation, path traversal prevention).

### 9.2 Store Injection

The `StoreInjectionMiddleware` runs after auth middleware. It reads `scope["state"]["user_id"]`, constructs a pre-scoped `MutableMapping` via `PrefixedStore`, and attaches it to `scope["state"]["store"]`. Apps receive a store that "just works":

```python
# Inside an app endpoint — zero auth or storage awareness
async def get_preferences(request: Request):
    store = request.state.store
    return store.get("preferences", {})
```

The `Mall` pattern (`stores.py`) provides `stores[user_id][app_id][data_key]` access. The store factory is configurable:

```toml
# platform.toml
[storage]
backend = "filesystem"     # or "s3", "sqlite"
path = "./data"            # for filesystem: data/{user_id}/{app_id}/
```

### 9.3 Backend Implementations

| Backend | When | Config |
|---|---|---|
| `filesystem` | Development, small scale | `path = "./data"` |
| `sqlite` | Single-VPS production | `path = "./data/enlace.db"` |
| `s3` | Cloud production, large files | `bucket`, `prefix`, AWS credentials |

Switching backends requires **no app code changes** — only `platform.toml` updates. This is the core value of the `dol` / `MutableMapping` abstraction.

---

## 10. CLI Interface

enlace uses `argh` for CLI dispatch, following the `python-package-architecture` conventions.

### 10.1 Top-Level Commands

```bash
# ── Serving ──
enlace serve                          # Start both servers (dev mode)
enlace serve --mode prod              # Production mode (no reload)
enlace serve --backend-only           # Backend only
enlace serve --frontend-only          # Frontend only

# ── Discovery & Config ──
enlace show-config                    # Resolved config, all apps
enlace show-config --verbose          # With provenance annotations
enlace show-config --json             # Machine-readable
enlace check                          # Validate config, check conflicts
enlace list-apps                      # App names, routes, access levels

# ── Build ──
enlace build                          # Build all frontends with package.json
enlace build <app_name>               # Build one app's frontend

# ── Deployment ──
enlace generate caddyfile             # Generate Caddyfile from config
enlace generate systemd               # Generate systemd unit files
enlace deploy                         # rsync + systemctl reload (configurable)

# ── Auth Utilities ──
enlace hash-password                  # Hash a shared password (Argon2id)
enlace create-session-secret          # Generate session secret

# ── Scaffolding ──
enlace new <app_name>                 # Create app directory with convention files
enlace init                           # Create platform.toml + apps/ in current dir
```

### 10.2 CLI Architecture

```python
# enlace/__main__.py
import argh
from enlace import __all__
import enlace

_dispatch_funcs = [
    enlace.serve,
    enlace.show_config,
    enlace.check,
    enlace.list_apps,
    enlace.build,
    enlace.new,
    enlace.init,
    enlace.hash_password,
    enlace.create_session_secret,
]

def main():
    parser = argh.ArghParser()
    parser.add_commands(_dispatch_funcs)
    parser.add_commands(
        [enlace.generate_caddyfile, enlace.generate_systemd],
        namespace="generate"
    )
    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass
    parser.dispatch()

if __name__ == "__main__":
    main()
```

### 10.3 Entry Point

```toml
# pyproject.toml
[project.scripts]
enlace = "enlace.__main__:main"
```

---

## 11. Observability

### 11.1 Structured Logging

Use `structlog` for JSON-structured logging in production, human-readable in development. A pure ASGI middleware logs every request with: timestamp, method, path, status, duration, app name, user ID (if authenticated).

See `deployment_observability.md` §"Observability" for the `structlog` configuration and middleware pattern.

### 11.2 Analytics Injection

Platform-level analytics (Plausible, Google Analytics) are injected into HTML responses via middleware — apps don't include tracking scripts. Configurable via `platform.toml [observability] analytics_script`.

### 11.3 Action Log

A structured action log (timestamp, user, app, action, metadata) stored via the same `MutableMapping` backend used for user data. This is opt-in per app.

---

## 12. Deployment

### 12.1 Development

```bash
pip install enlace
cd my-project
enlace init              # Creates platform.toml + apps/
enlace serve             # Starts both servers with hot reload
```

Hot reload: Uvicorn's `--reload` for backend. Frontend assets served directly (no build step needed for static HTML). See `deployment_observability.md` §"Hot reload" for the analysis of reload strategies.

### 12.2 Production (DigitalOcean VPS)

The deployment workflow:

```bash
enlace generate caddyfile > Caddyfile     # Generate reverse proxy config
enlace generate systemd > enlace.service  # Generate process management
enlace deploy                              # rsync to server + reload
```

**Stack:**
- **Caddy** for TLS termination, static file serving, reverse proxy. Automatic HTTPS via Let's Encrypt.
- **systemd** for process management (`Type=notify`, `WatchdogSec`, `Restart=on-failure`).
- **Uvicorn** with `--workers 2` for the backend process.

See `deployment_observability.md` §"Deployment" for the full systemd unit template, Caddy config, and zero-downtime reload strategy.

### 12.3 App Graduation

When an app outgrows the platform, it can be extracted and deployed independently. The platform is a launch pad, not a prison. The app's code is already self-contained — it just needs its own domain and process. See `deployment_observability.md` §"Graduating apps" for the extraction checklist.

---

## 13. Implementation Phases

### Phase 1: Foundation (MVP)

**Goal:** `enlace serve` runs a multi-app backend from a directory of Python modules.

- [ ] `base.py` — `AppConfig` and `PlatformConfig` Pydantic models
- [ ] `discover.py` — `ConventionDiscoverer`: walk `apps/`, find entry points, load TOML overrides, validate, detect conflicts
- [ ] `compose.py` — `build_backend()`: import modules, mount/include based on app type, apply `cascade_lifespan`
- [ ] `serve.py` — `serve()`: start Uvicorn with the composed app (dev mode with `--reload`)
- [ ] `__main__.py` — CLI with `serve`, `show-config`, `check`, `list-apps`
- [ ] `__init__.py` — facade exposing top-level commands
- [ ] `pyproject.toml` — package metadata, `enlace` script entry point
- [ ] Tests for discovery, composition, config resolution

**Acceptance criteria:** Running `enlace serve` in a directory with `apps/foo/server.py` (containing a FastAPI app) serves it at `localhost:8000/api/foo/`.

### Phase 2: Frontend + Auth

**Goal:** Serve frontend assets alongside backends. Gate apps behind shared passwords.

- [ ] `frontend.py` — `SPAStaticFiles`, launcher page generation
- [ ] `auth.py` — `PlatformAuthMiddleware` with path dispatch, shared-password login, signed cookies
- [ ] `inject.py` — HTML injection middleware (analytics, nav)
- [ ] `serve.py` — extend to run both backend and frontend servers (two subprocesses)
- [ ] Templates: login page, launcher page
- [ ] CLI: `hash-password`, `create-session-secret`

**Acceptance criteria:** `enlace serve` serves a React app at `/apps/my_app/` and gates `/api/dashboard/` behind a shared password. `enlace show-config` shows the access level for each app.

### Phase 3: User Data + Per-User Auth

**Goal:** Per-user sessions and data persistence through `MutableMapping`.

- [ ] `stores.py` — `PrefixedStore`, `Mall`, filesystem backend, SQLite backend
- [ ] `auth.py` — extend with per-user sessions, `SessionStore`
- [ ] `StoreInjectionMiddleware` — inject pre-scoped stores into ASGI scope
- [ ] CSRF protection via `starlette-csrf`
- [ ] WebSocket Origin validation

**Acceptance criteria:** A `protected:user` app can read `request.state.store["preferences"]` and it returns per-user data without importing any auth or storage code.

### Phase 4: Deployment

**Goal:** One-command deployment to a VPS.

- [ ] `deploy.py` — Caddyfile generation, systemd unit generation, rsync + reload
- [ ] `generate` CLI namespace
- [ ] Structured logging middleware (`structlog`)
- [ ] Analytics injection configuration
- [ ] Documentation: README, deployment guide

**Acceptance criteria:** `enlace generate caddyfile && enlace deploy` puts a working multi-app site on `thorwhalen.com` with automatic HTTPS.

---

## 14. Dependencies

**Core (required):**

```toml
[project]
dependencies = [
    "fastapi",
    "uvicorn[standard]",
    "pydantic",
    "pydantic-settings",
    "argh",
    "itsdangerous",      # Signed cookies
    "structlog",
]
```

**Optional:**

```toml
[project.optional-dependencies]
auth = ["argon2-cffi", "starlette-csrf", "authlib", "httpx"]
storage = ["dol"]
deploy = ["fabric"]     # For remote deployment
dev = ["pytest", "httpx", "pytest-asyncio", "honcho"]
```

**System:**
- Python 3.11+
- Node.js (optional, only for apps with `package.json`)
- Caddy (production only)

---

## 15. Key Design Decisions & Rationale

### Why `mount()` over microservices?

In-process mounting has zero network overhead, shared connection pools, single-process debugging, and one log stream. At 10–50 apps, the performance cost is negligible (~10–50µs routing overhead). Extract to separate processes only when needed (CPU-intensive, conflicting deps). See `asgi_composition.md` §9.

### Why cookie sessions over JWT?

For a single-domain platform, cookie sessions are simpler, more secure (`HttpOnly` prevents XSS theft), and immediately revocable. JWTs add token size overhead and refresh complexity without providing benefits when the auth server and resource server are the same process. See `auth_cross_cutting.md` Part II.

### Why TOML over YAML?

TOML is the Python ecosystem standard (PEP 518, `pyproject.toml`). Python 3.11+ includes `tomllib` in the standard library. YAML requires a third-party parser and has well-known footguns (implicit type coercion, Norway problem). See `convention_over_configuration.md` §6.

### Why Caddy over Nginx?

Automatic HTTPS with zero configuration. Native WebSocket proxying. Simpler config syntax. See `deployment_observability.md` §"Caddy wins".

### Why `argh` for CLI?

Follows established conventions (see `python-package-architecture` skill). Functions are commands. Type annotations are argument types. Docstrings are help text. Zero boilerplate.

### Why `MutableMapping` for storage?

Idiomatic Python (`store[key] = val`). Backend-agnostic by design. Composes naturally via key transforms (`PrefixedStore`, `wrap_kvs`). Aligns with `dol` patterns. See `user_data_persistence.md` and the `python-coding-standards` skill §"Mapping and MutableMapping for Storage".

---

## 16. Non-Goals

- Multi-tenant hosting for other developers
- Kubernetes or container orchestration
- Real-time collaboration
- Mobile-native apps
- CI/CD pipeline management (handled by `reci` separately)
- Framework lock-in — apps should be extractable and deployable independently

---

## 17. Security Checklist

These items are non-negotiable for any deployment:

- [ ] Path normalization before every auth check (prevent `//`, `/../`, URL-encoding bypasses)
- [ ] Deny-by-default for unmatched paths
- [ ] `HttpOnly; Secure; SameSite=Lax` on all auth cookies
- [ ] Argon2id hashing for shared passwords (never plaintext in config)
- [ ] `hmac.compare_digest()` for all credential comparisons (timing attack prevention)
- [ ] Strip incoming `X-User-ID` / `X-Forwarded-User` headers (prevent spoofing)
- [ ] CSRF protection for state-changing requests (signed double-submit cookie)
- [ ] WebSocket Origin validation against explicit allowlist
- [ ] HTTPS in production (`HTTPSRedirectMiddleware`)
- [ ] Never use `BaseHTTPMiddleware` (terminal bugs with exceptions and contextvars)

See `auth_cross_cutting.md` Part III for the full security analysis, real CVE examples, and bypass prevention patterns.

---

## 18. Interfaces for AI Agents

enlace is designed to be operated by AI agents (Claude skills, Claude Code, etc.) as well as humans. The key affordances:

- **`enlace show-config --json`** — machine-readable resolved configuration, including provenance annotations
- **`enlace check --json`** — structured validation output (errors, warnings)
- **`enlace new <app_name>`** — scaffolding that creates a valid app directory, ready to modify
- **TOML configuration** — simple, parseable format for agents to read and modify
- **Convention-based discovery** — agents can create apps by dropping files in the right place, no registration step needed
- **Clear error messages** — every error names the convention violated and suggests a fix
- **This specification** — agents should be given this document and the supporting research docs in `misc/docs/` as context

The `CLAUDE.md` file at the project root should instruct agents to:
1. Read this spec for architecture understanding
2. Run `enlace show-config` to understand current state
3. Run `enlace check` before and after changes
4. Consult `misc/docs/` for detailed rationale on any subsystem

---

## 19. Glossary

| Term | Definition |
|---|---|
| **App** | A unit of functionality: backend (Python), frontend (JS/HTML), or both |
| **Mount** | Attaching a sub-application to a prefix on the parent ASGI app |
| **Mall** | A mapping of mappings — `stores[user_id]` returns a per-user `MutableMapping` |
| **PrefixedStore** | A `MutableMapping` wrapper that prepends a key prefix transparently |
| **Access level** | One of `local`, `public`, `protected:shared`, `protected:user` |
| **Convention** | A rule that derives configuration from structure (e.g., directory name → route) |
| **Provenance** | Where a resolved config value came from (convention, file:line, env var, CLI flag) |
| **Cascade lifespan** | Forwarding ASGI startup/shutdown events to mounted sub-apps |
| **qh** | Tool for wrapping Python functions as HTTP endpoints (FastAPI routes) |
| **dol** | Data Object Layer — `MutableMapping`-based storage abstraction library |

---

*End of specification.*
