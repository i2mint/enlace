# enlace — Agent Instructions

## Core Principles

These two principles govern ALL changes to enlace. Every PR, feature, and
suggestion must respect both. They are in tension — the design challenge is
balancing them.

### 1. Apps should not need to change

enlace wraps apps from the outside. Apps don't import enlace, don't depend
on it, and don't know it exists. All aggregation logic (discovery, routing,
CORS, static serving) lives in enlace, not in the app.

When enlace encounters an app that's hard to mount:
- **First**: solve it on the enlace side (app.toml config, env vars, middleware)
- **Second**: suggest a minimal app change that preserves standalone operation
- **Last resort**: suggest an app change that breaks standalone — but flag it explicitly

### 2. Enlaced apps must still work alone

An app that works standalone today must still work standalone after being
enlaced. When we suggest changes to an app, those changes MUST preserve
the app's ability to run independently.

The pattern: **env-var with current value as default**.

```python
# GOOD: works standalone AND under enlace
import os
if not os.environ.get('ENLACE_MANAGED'):
    app.add_middleware(CORSMiddleware, ...)

# BAD: breaks standalone
# (CORSMiddleware deleted entirely)
```

```typescript
// GOOD: standalone uses localhost, enlace overrides at build time
const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000/api";

// BAD: breaks standalone
const API_BASE = "/api/s_conditions";
```

### Zero coupling

Apps do not import enlace. The dependency graph:
- enlace depends on FastAPI, Uvicorn, Pydantic, argh
- Apps depend on their own domain libs (FastAPI, numpy, etc.)
- There is NO dependency from apps to enlace

enlace provides services to apps via:
- ASGI scope injection (auth, store) — apps read `request.state.store`, not `import enlace.stores`
- Environment variables (`ENLACE_MANAGED`) — apps can condition on this but don't have to
- Convention (filesystem layout, app.toml) — external to app code

## Architecture

Read `misc/docs/enlace_spec.md` for the full architecture and design rationale.

```
enlace/
├── base.py        # Pydantic models: AppConfig (mode, process fields), PlatformConfig
├── util.py        # Pure helpers: derive_display_name, derive_route_prefix, is_skippable
├── discover.py    # ConventionDiscoverer: walks apps/, reads app.toml, detects types
├── compose.py     # build_backend(): mounts ASGI sub-apps, proxy routes, static files
├── proxy.py       # Lightweight ASGI reverse proxy for process/external backends (httpx)
├── supervise.py   # Dev-mode asyncio process supervisor (health checks, restart, logs)
├── diagnose.py    # diagnose_app(): scan an app dir for enlace compatibility issues
├── serve.py       # Orchestrates gateway Uvicorn + supervised process-mode children
├── __main__.py    # CLI via argh.dispatch_commands
├── __init__.py    # Public API facade
└── tests/         # Unit tests (test_base, test_discover, test_compose, test_supervise)
```

**Data flow (asgi-only):** `PlatformConfig.from_toml()` → `discover()` →
`check_conflicts()` → `build_backend(config)` → `uvicorn --factory`

**Data flow (mixed-mode):** `discover()` → partition by `mode` →
gateway Uvicorn (asgi + proxy routes) + `ProcessSupervisor` (process-mode children)

### App Modes

The `mode` field on `AppConfig` determines how an app is served:

| Mode | Description | Lifecycle |
|------|-------------|-----------|
| `asgi` | Import + mount on gateway FastAPI (default) | In-process |
| `process` | Spawn as child, health-check, restart, proxy | Supervised subprocess |
| `external` | Route to pre-existing upstream | No lifecycle management |
| `static` | Serve files from a directory | StaticFiles mount |

`mode` is orthogonal to `app_type` (asgi_app/functions/frontend_only).
`app_type` describes what was detected; `mode` describes how to run it.

## Before Making Changes

- Run `enlace show-config` to understand current state
- Run `enlace check` to validate config before and after changes

## Critical Rules

- **NEVER use `BaseHTTPMiddleware`** — it has terminal bugs (exception swallowing, ContextVar corruption). Use pure ASGI middleware (three-callable pattern) only.
- **Mount on `FastAPI()` directly**, never on `APIRouter` — known framework bug.
- **Discovery must never silently swallow ImportErrors** — distinguish "module doesn't exist" from "module has broken import".
- **All middleware must be pure ASGI** (scope, receive, send pattern).
- **Conflict detection is fail-fast** — report ALL conflicts, don't stop at first.
- **CORS on parent only** — sub-apps must not add their own CORSMiddleware. If they do, enlace still works (MEDIUM issue, not a blocker), but the diagnostic flags it.
- **`ENLACE_MANAGED=1`** — set by `build_backend()` and passed to supervised children so sub-apps can condition on it.
- **Suggestions to app developers must preserve standalone operation** — use the env-var-with-default pattern, never suggest changes that break the app's ability to run alone.
- **Process-mode apps are NOT imported** — if `app.toml` declares `mode="process"`, discovery skips Python introspection entirely. The app may not even be Python.
- **Dev supervisor is pure asyncio** — no external dependencies for process supervision. Health checks use stdlib `asyncio.open_connection`. Production supervision is delegated to systemd (future).
- **Reverse proxy requires httpx** — `pip install enlace[process]`. Import is lazy; asgi-only users don't need it.

## Research Docs

Consult these before modifying subsystems:

| Subsystem | Document |
|-----------|----------|
| App mounting, middleware | `misc/docs/asgi_composition__*.md` |
| Auth middleware | `misc/docs/auth_cross_cutting__*.md` |
| Discovery, config | `misc/docs/convention_over_configuration__*.md` |
| Deployment, logging | `misc/docs/deployment_observability__*.md` |
| Frontend serving | `misc/docs/frontend_serving__*.md` |
| Data persistence | `misc/docs/user_data_persistence__*.md` |
| Design principles | `misc/docs/design_principles__*.md` |
| Process orchestration | `misc/docs/Process orchestration and multi-backend composition for enlace.md` |
| ASGI assumption audit | `misc/docs/audit_backend_generality.md` |
| Generalization plan | `misc/docs/Enlace Generalization Plan*.md` |

## Testing

```bash
pytest enlace/tests/     # Unit tests
pytest tests/            # Integration tests
```
