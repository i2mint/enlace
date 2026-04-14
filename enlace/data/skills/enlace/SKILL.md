---
name: enlace
description: >
  Use when working with the enlace multi-app platform — creating apps, configuring
  platform.toml or app.toml, diagnosing discovery issues, understanding conventions,
  or serving/deploying apps. Triggers on: enlace CLI commands, apps/ directory
  structures, platform.toml/app.toml files, multi-app ASGI composition, or when the
  user mentions enlace, app discovery, app mounting, or personal app platform.
---

# enlace — Multi-App Platform

enlace discovers Python modules and React apps in a directory, mounts them as
sub-applications under a single ASGI server, and serves them.

**enlace is not a framework.** Apps don't import it, don't depend on it, and
don't know it exists. You write a standard FastAPI app (or plain Python
functions with type hints). enlace discovers it from the outside, mounts it
alongside other apps, and serves them all — without touching your code.

**Two principles:**
1. **Apps should not need to change** — all aggregation logic lives in enlace.
   When an app is hard to mount, prefer enlace-side config (app.toml, env vars)
   over app code changes.
2. **Enlaced apps must still work alone** — if we do suggest changes, they must
   preserve standalone operation. Pattern: env-var with current value as default.

## Core Concept

Drop files in `apps/`, enlace finds and serves them:

```
apps/
├── my_tool/
│   └── server.py          # has `app = FastAPI()` → served at /api/my_tool
├── dashboard/
│   ├── server.py           # backend
│   └── frontend/
│       └── index.html      # SPA assets
├── calculator/
│   └── server.py           # has typed functions, no `app` → auto-wrapped
└── blog/
    └── frontend/
        └── index.html      # frontend-only, no backend
```

Everything enlace infers is inspectable (`enlace show-config`) and overridable
(via TOML or CLI flags).

## CLI Commands

```bash
enlace serve                          # Start backend (dev mode, hot reload)
enlace serve --mode prod              # Production mode (2 workers)
enlace serve --port 9000              # Custom port
enlace serve --app-dirs "/a,/b"       # Serve apps from specific directories
enlace show-config                    # Resolved config with provenance
enlace show-config --json             # Machine-readable
enlace show-config --verbose          # Show where each value came from
enlace check                          # Validate config, check route conflicts
enlace list-apps                      # Table: name, route, type, access
```

## Creating an App

### Standalone ASGI App (most common)

Create `apps/{name}/server.py` with an `app` attribute:

```python
# apps/my_tool/server.py
from fastapi import FastAPI

app = FastAPI()

@app.get("/hello")
def hello():
    return {"message": "Hello from my_tool"}
```

This gets mounted at `/api/my_tool/`. The `app` attribute name is configurable.

### Function Collection (no FastAPI needed)

If the entry module has typed public functions but no `app` attribute, enlace
auto-wraps them as endpoints:

```python
# apps/calculator/server.py
def add(a: int, b: int) -> dict:
    return {"result": a + b}

def multiply(a: float, b: float) -> dict:
    return {"result": a * b}
```

Functions become POST endpoints: `/api/calculator/add`, `/api/calculator/multiply`.
Simple-type parameters are query params.

### Frontend-Only App

Just a `frontend/` directory with `index.html`:

```
apps/blog/
└── frontend/
    └── index.html
```

No backend mounted. Assets served at `/apps/blog/` (in production via Caddy).

## Discovery Conventions

| What | Convention | Override key in `app.toml` |
|------|-----------|---------------------------|
| Route prefix | `/api/{directory_name}` | `route` |
| Backend entry | First of `server.py`, `app.py`, `main.py` | `entry_point` |
| ASGI app object | Attribute named `app` | `app_attr` |
| Frontend assets | `frontend/` with `index.html` | `frontend_dir` |
| Display name | Dir name, `_` → space, title-cased | `display_name` |
| Access level | `local` (default) | `access` |
| Skip directory | Starts with `_` or `.` | — |

## Multi-Source App Discovery

enlace can discover apps from multiple locations — you don't need to move
existing projects into a single `apps/` folder.

Two source types:
- **`apps_dirs`**: directories that CONTAIN app subdirectories (walk children)
- **`app_dirs`**: individual directories that ARE apps (discover directly)

### Enlacing existing projects

```toml
# platform.toml
[platform]
apps_dirs = ["apps"]
app_dirs = [
    "/Users/thor/projects/chord_analyzer",
    "/Users/thor/projects/todo_app",
]
```

Or via CLI:
```bash
enlace serve --app-dirs "/path/to/chord_analyzer,/path/to/todo_app"
enlace serve --apps-dirs "apps,/path/to/more_apps"
```

Symlinks also work: `ln -s /path/to/my_app apps/my_app` and enlace discovers
it transparently.

App names must be globally unique across all sources. Duplicates are caught
by `enlace check`.

## Configuration

### `platform.toml` (global, in project root)

```toml
[platform]
apps_dirs = ["apps"]                   # container directories (walk children)
app_dirs = []                          # individual app directories
domain = "localhost"
backend_port = 8000

[conventions]
entry_points = ["server.py", "app.py", "main.py"]
app_attr = "app"
frontend_dir = "frontend"
```

The legacy `apps_dir = "apps"` (scalar) is still supported for backward compat.

### `app.toml` (per-app, in app directory)

```toml
route = "/api/custom-route"
access = "public"
display_name = "My Custom App"
entry_point = "application.py"
app_attr = "my_app"
```

### Override Precedence (lowest → highest)

```
hardcoded defaults → filesystem conventions → app.toml → platform.toml → env vars → CLI flags
```

Environment variables: `ENLACE_APPS_DIRS`, `ENLACE_APP_DIRS` (pathsep-delimited).

## App Types

| Type | Detection | Mounting |
|------|-----------|---------|
| `asgi_app` | Module has `app` attribute (callable) | `parent.mount(prefix, sub_app)` |
| `functions` | No `app` attr, has typed public functions | Auto-wrapped as APIRouter |
| `frontend_only` | No backend entry, has `frontend/index.html` | Static file serving only |

## Diagnosing Issues

**App not discovered?**
1. Run `enlace show-config --verbose` — is the app listed?
2. Check directory isn't prefixed with `_` or `.`
3. Check entry point file exists (`server.py`, `app.py`, or `main.py`)
4. Check for import errors — enlace intentionally surfaces them (won't silently skip)

**Route conflict?**
- `enlace check` reports ALL conflicts at once with both app names
- Fix by changing `route` in one app's `app.toml`

**Wrong app type detected?**
- Run `enlace show-config --verbose` to see detection reason
- Ensure your `app = FastAPI()` is at module level (not inside a function)
- For function collections, ensure functions have type annotations

## Access Levels

| Level | Description |
|-------|-------------|
| `local` | Development only (default) |
| `public` | Open to everyone |
| `protected:shared` | Single shared password gate |
| `protected:user` | Per-user accounts |

Set in `app.toml` with `access = "protected:shared"` etc.

## Workflow

```bash
# 1. Initialize (creates platform.toml + apps/)
enlace init

# 2. Create an app
mkdir -p apps/my_app
# Write apps/my_app/server.py

# 3. Verify discovery
enlace show-config
enlace check

# 4. Serve
enlace serve
# → http://localhost:8000/api/my_app/
```

Always run `enlace check` after making changes to catch conflicts early.
