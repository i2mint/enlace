# Design Principles: Zero Coupling and Standalone Preservation in Multi-App Composition

**Context:** enlace composes multiple web apps under a single domain. This
document explains the two design principles that govern how enlace interacts
with apps, why they matter, and how to apply them in practice.

---

## 1. Zero Coupling: Apps Don't Import enlace

### The principle

Apps do not depend on, import, or reference enlace. enlace wraps apps from the
outside. The dependency graph has no arrow from app to enlace:

```
enlace (the platform)          your app
├── fastapi                    ├── fastapi
├── uvicorn                    ├── pandas (or whatever)
├── pydantic                   └── ... your domain libs
└── argh
                               ← no dependency here
```

### Why this matters

- **Extractability**: An app that doesn't import enlace can be extracted and
  deployed independently at any time. The spec calls this "graduating" — enlace
  is a launch pad, not a prison (spec §15, §16).

- **Framework freedom**: Apps are standard ASGI applications. They work with
  any ASGI server (uvicorn, gunicorn, daphne). They can be tested with
  `starlette.testclient.TestClient` without enlace.

- **No version coupling**: enlace can be upgraded without touching app code.
  Apps can be upgraded without touching enlace.

### How enlace provides services without coupling

| Service | Mechanism | App sees |
|---------|-----------|----------|
| CORS | Parent middleware | Nothing (transparent) |
| Auth | ASGI scope injection | `request.state.user` (standard Starlette) |
| Data store | ASGI scope injection | `request.state.store` (standard `MutableMapping`) |
| Logging | Parent middleware | Nothing (transparent) |
| Analytics | HTML injection | Nothing (transparent) |
| Managed flag | Environment variable | `os.environ.get('ENLACE_MANAGED')` (optional) |

Apps consume standard Python abstractions (`MutableMapping`, `request.state`,
`os.environ`) — never enlace-specific APIs.

### The ENLACE_MANAGED convention

`build_backend()` in `compose.py` sets `os.environ["ENLACE_MANAGED"] = "1"` when
composing sub-apps. This allows apps to conditionally skip behavior that conflicts
with enlace (e.g., their own CORS middleware):

```python
import os
if not os.environ.get('ENLACE_MANAGED'):
    app.add_middleware(CORSMiddleware, ...)
```

This is an **opt-in convention**, not a dependency. An app that ignores
`ENLACE_MANAGED` entirely still works — enlace just has to deal with (e.g.)
duplicate CORS headers.

---

## 2. Standalone Preservation: Enlaced Apps Must Still Work Alone

### The principle

An app that works standalone today must still work standalone after any changes
suggested by enlace (its diagnostic tool, its documentation, or its contributor
skills). Changes that break standalone operation are a last resort, and must be
flagged explicitly.

### Why this matters

- **Development workflow**: Developers run apps standalone during development
  (`uvicorn server:app --reload`). Breaking this forces them into enlace-only
  development, which is slower and more complex.

- **Testing**: Apps tested in isolation (unit tests, integration tests with
  TestClient) must continue to pass after enlace-suggested changes.

- **Opt-out**: If the developer decides to un-enlace an app, it should just
  work with no code changes.

### The env-var-with-default pattern

When an app has a hardcoded value that enlace needs to override, the pattern is:
**make it configurable with the current value as the default**.

```typescript
// BEFORE: works standalone, broken under enlace
const API_BASE = "http://localhost:8000/api";

// AFTER: works standalone (no env var) AND under enlace (env var set at build time)
const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000/api";
```

```python
# BEFORE: works standalone, broken under enlace (double CORS headers)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"])

# AFTER: works standalone AND under enlace
import os
if not os.environ.get('ENLACE_MANAGED'):
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"])
```

The key insight: the **default value is always the standalone value**. enlace
provides the override through environment variables or build-time configuration.

### Classification of fixes

When the diagnostic tool (`enlace diagnose`) reports an issue, it classifies
the suggested fix:

| Fix type | Changes app code? | Preserves standalone? | Example |
|----------|-------------------|-----------------------|---------|
| Enlace-side | No | Yes (trivially) | `app.toml` with `entry_point = "backend/main.py"` |
| Build-time | No | Yes (trivially) | `NEXT_PUBLIC_API_BASE=/api/foo npm run build` |
| App change (preserving) | Yes | Yes | Env-var with current value as default |
| App change (breaking) | Yes | **No** | Hardcoding a subpath — avoid this |

The `Issue` dataclass has a `breaks_standalone` flag. When True, the diagnostic
output warns the user explicitly.

### What "preserves standalone" means precisely

After applying the suggested fix:
1. `uvicorn server:app` (or `npm run dev`) starts without errors
2. The app serves at its original localhost URL
3. All API calls from the frontend reach the backend
4. Tests pass without modification
5. No environment variables need to be set for standalone mode

---

## 3. The Tension and How to Resolve It

The two principles are in tension: sometimes an app genuinely cannot work under
enlace without *some* change (e.g., a hardcoded `http://localhost:8000` URL in
JavaScript). In these cases, the resolution order is:

1. **Can enlace handle it without any app change?** (e.g., entry point in a
   subdirectory → `app.toml`). If yes, do that.

2. **Can the app change preserve standalone?** (e.g., env-var with default).
   If yes, suggest that.

3. **Is the change small and clearly beneficial?** (e.g., one-line env-var
   pattern). If yes, it's worth it — the app becomes more configurable for
   *any* deployment context, not just enlace.

4. **Would the change break standalone?** Only as a last resort. Flag it with
   `breaks_standalone=True`. Explain why and offer alternatives.

### What enlace should do better over time

Some patterns that currently require app changes could be handled by enlace:

- **URL rewriting middleware**: Rewrite `http://localhost:8000` → `/api/foo` in
  HTML/JS responses at serve time. Complex but would eliminate the most common
  CRITICAL issue.

- **CORS header deduplication**: Strip duplicate `Access-Control-*` headers
  from sub-app responses. Would downgrade sub-app CORS from a problem to a
  non-issue.

- **Reverse proxy mount**: For SSR frontends (Next.js, Nuxt), proxy to a
  running Node.js process. Would eliminate the static-export requirement.

These are future features, not current capabilities. The diagnostic tool should
track what enlace *can* handle today and what it aspires to handle.

---

## 4. Applying These Principles

### For enlace contributors

When adding features to enlace:
- Services to apps go through ASGI scope or environment, never imports
- New middleware must be pure ASGI on the parent app
- Diagnostic suggestions must preserve standalone (use `breaks_standalone` flag)
- Documentation must explain the coupling model

### For the diagnostic tool

When reporting issues:
- Prefer enlace-side fixes (app.toml, build config) over app changes
- When app changes are needed, use the env-var-with-default pattern
- Show the exact code change with before/after
- Flag `breaks_standalone` when unavoidable

### For skill instructions

When guiding Claude Code users:
- Never suggest deleting CORS middleware (suggest conditional instead)
- Never suggest hardcoding enlace-specific paths (suggest env-var with default)
- Always verify standalone operation after suggesting changes
- Present enlace-side fixes separately from app changes
