---
name: enlace-dev
description: >
  Use when developing or modifying the enlace package itself — adding features,
  fixing bugs, extending the ASGI composition, adding middleware, or working on
  any module in the enlace/ source directory. Triggers on: editing enlace source
  files (base.py, discover.py, compose.py, serve.py, auth.py, stores.py),
  implementing spec phases 2-4, or when the user says "add X to enlace" or
  "implement Y in enlace".
---

# Developing enlace

enlace is a multi-app ASGI platform. This skill covers the critical rules,
architecture, and patterns needed to modify the enlace codebase safely.

## Design Principles

These two principles govern ALL changes. Every feature, fix, and diagnostic
suggestion must respect both:

### 1. Zero coupling — apps don't import enlace

enlace wraps apps from the outside. Apps depend on their own domain libs
(FastAPI, numpy, etc.), never on enlace. enlace provides services via:
- ASGI scope injection (auth, store) — not imports
- Environment variables (`ENLACE_MANAGED`) — optional, apps can ignore it
- Convention (filesystem layout, app.toml) — external to app code

When adding features, NEVER create patterns that require apps to
`import enlace` or depend on enlace-specific APIs.

### 2. Preserve standalone operation

When enlace (or its diagnostic tool) suggests changes to an app, those
changes MUST preserve the app's ability to run independently. The pattern:
**env-var with current value as default**.

```python
# GOOD: works standalone AND under enlace
if not os.environ.get("ENLACE_MANAGED"):
    app.add_middleware(CORSMiddleware, ...)

# BAD: breaks standalone
# (CORSMiddleware deleted entirely)
```

When writing diagnostic messages, fix suggestions, or documentation:
- Prefer enlace-side solutions (app.toml, build-time env vars) over app changes
- When app changes are needed, always use the env-var-with-default pattern
- Flag any suggestion that would break standalone with `breaks_standalone=True`

See `misc/docs/design_principles__*.md` for the full rationale.

## Architecture

```
enlace/
├── base.py        # Pydantic models: AppConfig, PlatformConfig, ConventionsConfig
├── util.py        # Pure helpers: derive_display_name, derive_route_prefix, is_skippable
├── discover.py    # ConventionDiscoverer: walks apps/, detects types, loads TOML
├── compose.py     # build_backend(): mounts sub-apps, cascade_lifespan
├── diagnose.py    # diagnose_app(): scan an app dir for enlace compatibility issues
├── serve.py       # Uvicorn subprocess orchestration, signal forwarding
├── __main__.py    # CLI: one dispatch list of verb functions (cw)
├── __init__.py    # Public API facade
└── tests/         # Unit tests (test_discover.py, test_compose.py)
```

**Data flow:** `PlatformConfig.from_toml()` → `ConventionDiscoverer.discover()` →
`config.check_conflicts()` → `build_backend(config)` → `uvicorn --factory`

## Critical Rules

### Never use BaseHTTPMiddleware

It has terminal, unfixable bugs: exception swallowing across mounted sub-apps,
ContextVar propagation corruption, synchronous execution of background tasks.
The Starlette maintainer has called it unfixable. Always use pure ASGI middleware:

```python
# YES: Pure ASGI middleware
class MyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # your logic here
            pass
        await self.app(scope, receive, send)


# NO: BaseHTTPMiddleware — NEVER use this
```

### Mount on FastAPI directly, not APIRouter

`app.mount()` only works on the top-level FastAPI instance. Mounting on an
APIRouter silently fails (FastAPI issues #4194, #10180).

### Never silently swallow import failures during discovery

If a module file exists but fails to import (syntax error, missing dep), that is
a real error. Propagate it. Only skip directories that have no entry point file.
This prevents the Celery autodiscover anti-pattern.

Reporting is not swallowing. `discover_apps(..., on_import_error="record")`
records the failure on `AppConfig.import_error` and carries on, so the
diagnostic verbs can name the broken app instead of dying on it — `enlace
doctor` still exits nonzero, with a `FAIL` check. `serve` keeps the default
`"raise"`. Never make `"record"` the default: a gateway that boots pretending a
broken app is fine IS the anti-pattern. Note the net is `Exception`, not
`ImportError` — a module runs arbitrary code at import (the failure seen in
production was a `PermissionError`).

### Conflict detection: fail-fast, report ALL

`check_conflicts()` collects every duplicate route, not just the first. Users
need to see all conflicts at once to fix them in one pass.

### Starlette does NOT cascade lifespan events

Mounted sub-apps never receive startup/shutdown. The `cascade_lifespan` context
manager in compose.py works around this by iterating Mount routes and entering
their lifespan contexts. Any new mounting logic must preserve this.

### CORS on parent only

If a sub-app also adds CORS middleware, responses get duplicate headers. All
cross-cutting middleware (auth, CORS, logging, store injection) goes on the
parent FastAPI app exclusively.

## Research Documents

Detailed rationale, code patterns, and pitfalls live in `misc/docs/`:

| Module area | Consult |
|-------------|---------|
| compose.py, mounting, middleware | `asgi_composition__*.md` |
| auth.py, sessions, cookies | `auth_cross_cutting__*.md` |
| discover.py, config, show-config | `convention_over_configuration__*.md` |
| serve.py, deployment, logging | `deployment_observability__*.md` |
| frontend.py, SPA serving | `frontend_serving__*.md` |
| stores.py, PrefixedStore, Mall | `user_data_persistence__*.md` |
| Design philosophy, coupling, independence | `design_principles__*.md` |

Read the relevant doc before modifying its subsystem.

## Implementation Phases (from spec)

**Phase 1 (done):** Core discovery, composition, CLI, serve
**Phase 2:** frontend.py (SPAStaticFiles, launcher), auth.py (PlatformAuthMiddleware,
  shared-password login, signed cookies), inject.py (HTML injection), hash-password
  and create-session-secret CLI commands
**Phase 3:** stores.py (PrefixedStore, Mall, filesystem/SQLite backends),
  StoreInjectionMiddleware, per-user sessions, CSRF, WebSocket origin validation
**Phase 4:** deploy.py (Caddyfile/systemd generation), structlog middleware,
  analytics injection, `generate` CLI namespace

## Testing Pattern

```bash
pytest enlace/tests/     # Unit tests
pytest tests/            # Integration tests
```

Tests use `tmp_path` fixtures to create temporary app directories. Composition
tests use `starlette.testclient.TestClient` against the built app. Always test
both the happy path and error cases (import errors, conflicts, missing files).

## Adding a New Module

1. Create `enlace/{module}.py`
2. Add exports to `enlace/__init__.py`
3. Create `enlace/tests/test_{module}.py`
4. If it adds CLI commands: write each verb as a plain function in
   `enlace/__main__.py` — a thin wrapper over the public function you exported
   in step 2 — and add it to the single dispatch list at the bottom of
   `__main__.py`. **That list is the SSOT for the CLI surface**: a verb that is
   not in it does not exist. Do not introduce a CLI library anywhere else in
   the package; the single dispatch call is **`cw`** (MIT, zero runtime
   dependencies), and what you should be matching is the *shape* — plain
   functions, one list — not the dispatcher's API. Adding a verb must not
   require touching the dispatch call at all.
5. Run `enlace check` before and after to verify nothing breaks

### CLI grammar trap (proven — read before changing the dispatch call)

`build(app_name="", *, dry_run=False, ...)` is the live signature of `enlace
build`: a *defaulted positional*. Under `argh.dispatch_commands` — the call this
package used before the `cw` migration — it rendered `--app-name` as an *option*.
The identical signature under `argh.add_commands` raises
`ArgumentNameMappingError` ("not keyword-only but has a default value"), because
`add_commands` applies the post-0.30 `BY_NAME_IF_KWONLY` policy and
`dispatch_commands` does not.

The consequence for anyone replacing the dispatch call: an adapter whose rule is
"keyword-only parameters become options, defaulted positionals stay positional"
silently reinterprets `enlace build --app-name x` as the positional form
`enlace build x`. That is a same-exit-code, wrong-behaviour change, so tests
that only check "did it run" will not catch it.

**`cw` lands on the right side of this trap — but only on its default.**
`cw.dispatch(COMMANDS)` uses `cw.ARGH`, which reproduces `argh.dispatch_commands`'
rule (*a parameter with a default becomes an option*), so `--app-name` stayed an
option and `enlace build --help` did not move across the migration.
`convention=cw.MODERN` — and equally `cw.BY_NAME_IF_KWONLY` — is exactly the rule
described above and *would* turn `app-name` into the positional `enlace build
<app-name>`. That reinterpretation runs, exits 0, and silently means something
else. Verified on this signature, in both directions. **So: do not "modernise"
the convention.** The dispatch call carries no `convention=` argument, and that
absence is deliberate.

`main()` is `raise SystemExit(cw.dispatch(COMMANDS))`: `argh` exited by itself,
`cw` *returns* the exit code, and dropping the `SystemExit` turns every usage
error into exit 0 — invisible to every test that only checks "did it run".

Both halves are pinned by `tests/test_cli_surface.py`, which asserts that
`--app-name x` is accepted AND that `build x` is rejected, and that usage errors
exit 2. Both assertions were shown to fail under the wrong convention and the
missing `SystemExit` respectively before being committed. `enlace doctor` is also
load-bearing: downstream deploy smoke tests shell out to `enlace doctor --json`
and judge its output, so its grammar and its JSON shape are both contracts, and
that test pins them too.
