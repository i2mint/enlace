# enlace

Compose, serve, and deploy multiple web apps under a single domain.

**`enlace` is not a framework, it's a runtime orchestrator.** Apps don't import it, don't depend on it, and
don't know it exists. You write a standard FastAPI app (or plain Python
functions). `enlace` discovers it from the outside, mounts it alongside other
apps, and serves them all — without touching your code.

## Philosophy

**Apps don't depend on `enlace`.** `enlace` is an operator's tool, not a library
your app imports. Your app is a standard Python module with `app = FastAPI()`.
It runs standalone with `uvicorn server:app`. `enlace` just happens to know how
to find it, mount it at a route prefix, and serve it alongside other apps.
(See [Zero Coupling](misc/docs/design_principles__Zero_coupling_and_standalone_preservation_in_multi_app_composition.md#1-zero-coupling-apps-dont-import-enlace)
for how `enlace` provides services like auth and storage without creating a
dependency.)

```
enlace (the platform)          your app
├── fastapi                    ├── fastapi
├── uvicorn                    ├── pandas (or whatever you need)
├── pydantic                   └── ... your domain libs
└── argh
                               ← no arrow here: your app does NOT import enlace
```

**Two principles in tension:**

1. **Apps should not need to change.** All aggregation logic lives in `enlace`.
   When an app is hard to mount, prefer `enlace`-side config (app.toml, env vars)
   over app code changes.

2. **Enlaced apps must still work alone.** When changes are suggested, they
   preserve standalone operation. The pattern: env-var with current value as
   default — standalone uses the original, `enlace` overrides at build time.
   (See [Standalone Preservation](misc/docs/design_principles__Zero_coupling_and_standalone_preservation_in_multi_app_composition.md#2-standalone-preservation-enlaced-apps-must-still-work-alone)
   for the env-var-with-default pattern and fix classification.)

For the full rationale — including how these principles interact, where the
balance sits today, and what `enlace` aspires to handle better — see the
[Design Principles](misc/docs/design_principles__Zero_coupling_and_standalone_preservation_in_multi_app_composition.md) document.

## Using `enlace` with an AI agent

`enlace` ships with AI agent skills that let Claude Code (or any compatible
agent) handle the entire workflow through natural language:

```
"Add my_app to the platform"
"Can s_conditions be enlaced?"
"Diagnose /path/to/app and fix what you can"
"Serve all my apps"
```

### Install

```bash
pip install enlace
```

Skills are bundled with the package. To make them available to Claude Code:

```bash
# Link enlace's skills into your project (or globally)
skill link-skills "$(python -c 'import enlace; print(enlace.__path__[0])')"

# Or symlink manually
ln -s "$(python -c 'from enlace import skills_dir; print(skills_dir())')"/* .claude/skills/
```

### Available skills

| Skill | What it does | Trigger phrases |
|-------|-------------|-----------------|
| **enlace** | Create apps, configure platform.toml, understand conventions, serve | "add an app", "serve my apps", "configure enlace" |
| **enlace-diagnose** | Analyze an app for compatibility, suggest fixes that preserve standalone operation | "can this be enlaced?", "diagnose this app", "what needs to change?" |
| **enlace-dev** | Modify the enlace package itself — add features, fix bugs, extend middleware | "add X to enlace", "implement Y in enlace" |

### What the AI does for you

**Onboarding an existing app:**
The agent runs `enlace diagnose`, reads the report, and presents findings in
three tiers: `enlace`-side fixes (no app changes), app changes that preserve
standalone, and warnings. It proposes specific code changes and applies them
with your approval.

**Creating a new app:**
The agent scaffolds the directory structure, writes `server.py` with a FastAPI
app, optionally creates `frontend/index.html`, registers it in `platform.toml`,
runs `enlace check`, and starts serving.

**Day-to-day operations:**
The agent runs `enlace serve`, `enlace check`, `enlace show-config` as needed,
interprets the output, and explains what's happening.

## Under the hood

For those who want direct control, here's the CLI, Python API, and
configuration system that the skills use internally.

### Quick start

```bash
pip install enlace
```

```bash
# Create an app
mkdir -p apps/hello
cat > apps/hello/server.py << 'EOF'
from fastapi import FastAPI
app = FastAPI()

@app.get("/greet")
def greet(name: str = "world"):
    return {"message": f"Hello, {name}!"}
EOF

# Serve it
enlace serve
# → http://localhost:8000/api/hello/greet?name=Thor
```

### CLI

```bash
enlace serve              # Start backend (dev mode, hot reload)
enlace show-config        # Resolved config with provenance
enlace check              # Validate config, check route conflicts
enlace list-apps          # Table: name, route, type, access
enlace diagnose <dir>     # Analyze an app for enlace compatibility
```

### Python API

```python
from enlace import diagnose_app, discover_apps, build_backend

# Diagnose an app
report = diagnose_app("/path/to/my_app")
print(report)              # Human-readable report
print(report.is_enlaceable)  # True if no critical blockers

# Discover and compose
config = discover_apps()
app = build_backend(config)  # FastAPI app with all sub-apps mounted
```

### App discovery

enlace discovers apps by filesystem conventions:

```
apps/
├── my_tool/
│   └── server.py          # has `app = FastAPI()` → mounted at /api/my_tool
├── dashboard/
│   ├── server.py           # backend
│   └── frontend/
│       └── index.html      # served at /dashboard/
├── calculator/
│   └── server.py           # typed functions, no `app` → auto-wrapped as routes
└── blog/
    └── frontend/
        └── index.html      # frontend-only, no backend
```

| Convention | Default | Override in `app.toml` |
|-----------|---------|----------------------|
| Route prefix | `/api/{dir_name}` | `route` |
| Entry point | First of `server.py`, `app.py`, `main.py` | `entry_point` |
| ASGI app object | Attribute named `app` | `app_attr` |
| Frontend assets | `frontend/index.html` | `frontend_dir` |

Everything enlace infers is inspectable (`enlace show-config --verbose`) and
overridable via `app.toml`, `platform.toml`, environment variables, or CLI flags.

### Configuration

**`platform.toml`** (project root):

```toml
[platform]
apps_dirs = ["apps"]                # Directories containing app subdirs
app_dirs = ["/path/to/standalone"]  # Individual app directories
backend_port = 8000

[conventions]
entry_points = ["server.py", "app.py", "main.py"]
app_attr = "app"
frontend_dir = "frontend"
```

**`app.toml`** (per-app, in app directory):

```toml
route = "/api/custom-route"
entry_point = "backend/main.py"
access = "public"
display_name = "My App"
```

**Override precedence** (lowest → highest):

```
defaults → filesystem conventions → app.toml → platform.toml → env vars → CLI flags
```

### App types

| Type | How detected | How mounted |
|------|-------------|-------------|
| `asgi_app` | Module has callable `app` attribute | `parent.mount(prefix, sub_app)` |
| `functions` | No `app` attr, has typed public functions | Auto-wrapped as API routes |
| `frontend_only` | No backend entry, has `frontend/index.html` | Static file serving only |
