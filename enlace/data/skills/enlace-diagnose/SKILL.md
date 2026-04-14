---
name: enlace-diagnose
description: >
  Diagnose whether an app can be "enlaced" (mounted under the enlace multi-app
  platform). Identifies hardcoded URLs, CORS issues, SSR requirements, missing
  entry points, and other compatibility blockers. Produces a report with severity
  levels and actionable fix suggestions that preserve the app's ability to run
  standalone. Use when the user wants to bring an existing app into enlace, asks
  "can this app be enlaced?", "why won't my app work with enlace?", "diagnose
  this app", or when onboarding any new app. Also triggers on: "compatibility
  check", "what needs to change", "analyze this app for enlace", or when the
  user points at an app directory and asks about integration.
---

# Diagnose App Compatibility with enlace

This skill helps you analyze an app directory and produce a structured
compatibility report — identifying what blocks enlace integration, what
complicates it, and what could be cleaner.

## Core Principles

These principles MUST guide every suggestion you make:

### 1. Zero app changes is the ideal

enlace's goal is to mount apps without changing their code. All aggregation
logic should be external. When something can be solved on the enlace side
(via app.toml config, env vars at build time, or enlace middleware), prefer
that over modifying the app.

### 2. Preserve standalone operation

An app that works alone today must still work alone after any changes you
suggest. **Never suggest a fix that silently breaks standalone mode.**

The pattern for this is: **env-var with current value as default**.

```typescript
// BAD: breaks standalone
const API_BASE = "/api/s_conditions";

// GOOD: works standalone (no env var) AND under enlace (with env var)
const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000/api";
```

```python
# BAD: breaks standalone (can't serve its own frontend anymore)
# (deleted CORSMiddleware entirely)

# GOOD: works standalone AND under enlace
import os
if not os.environ.get('ENLACE_MANAGED'):
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"], ...)
```

### 3. Minimal changes, maximum configurability

When a change IS needed, make the smallest possible change — usually converting
a hardcoded value into an env-var-backed configurable with the original value as
the default. The developer should understand exactly what changed and why.

### 4. Tell the developer what's happening

For every suggestion, explain:
- What the issue is and why enlace cares
- Whether this is an enlace-side fix (app.toml, build config) or an app change
- Whether standalone operation is affected
- If the issue is tagged `breaks_standalone: true`, say so explicitly

## When to use

- User wants to bring an existing app under enlace
- User asks "can this be enlaced?" or "what needs to change?"
- User points at an app directory and asks about enlace compatibility
- Before migrating or onboarding any new app

## Step 1: Run the diagnostic tool

enlace ships `enlace diagnose` (CLI) and `enlace.diagnose_app()` (Python API).

```bash
# CLI — human-readable report
enlace diagnose /path/to/app_dir

# CLI — machine-readable
enlace diagnose /path/to/app_dir --json

# Exit code: 0 = no critical issues, 1 = has critical blockers
```

```python
# Python API
from enlace import diagnose_app

report = diagnose_app("/path/to/app_dir")
print(report)                    # Human-readable text
print(report.to_json())          # JSON
print(report.is_enlaceable)      # True if no CRITICAL issues
print(report.critical_count)     # Number of critical issues
```

Always run `diagnose_app()` or `enlace diagnose` first. Read the report before
doing anything else.

## Step 2: Interpret the report

The report classifies issues into four severity levels:

| Severity | Meaning | Action |
|----------|---------|--------|
| **CRITICAL** | Blocks enlace integration entirely | Must address before enlacing |
| **MEDIUM** | Works but with friction or risk | Should address |
| **LOW** | Works as-is but could be cleaner | Nice to have |
| **INFO** | Informational observation | No action needed |

### Classify each fix by WHO needs to change

| Fix type | Changes app code? | Example |
|----------|-------------------|---------|
| **enlace-side** | No | Create `app.toml` with `entry_point = "backend/main.py"` |
| **build-time** | No | `NEXT_PUBLIC_API_BASE=/api/foo npm run build` |
| **app change (preserving)** | Yes, but standalone still works | Add env-var with current value as default |
| **app change (breaking)** | Yes, breaks standalone | Hardcode a subpath — **avoid this** |

### Common issue categories

#### `hardcoded_url` (CRITICAL in frontend, MEDIUM in backend)

**Problem**: Hardcoded `http://localhost:XXXX` URLs.

**Independence-preserving fix for frontend**:
```typescript
// Current value becomes the default — standalone still works
const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000/api";
```
Then for enlace: `NEXT_PUBLIC_API_BASE=/api/{app_name} npm run build`

**Independence-preserving fix for vanilla HTML/JS** (papp-style):
```javascript
// Relative URLs work in both contexts if the API prefix matches
fetch("/api/my_app/data")
```

#### `subapp_cors` (MEDIUM)

**Problem**: Sub-app adds its own `CORSMiddleware`.

**Why MEDIUM, not CRITICAL**: enlace can still mount the app. The double headers
*may* confuse some browsers but often work fine. This is a "should fix" not a
blocker.

**Independence-preserving fix**:
```python
import os
if not os.environ.get('ENLACE_MANAGED'):
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"], ...)
```
enlace sets `ENLACE_MANAGED=1` in its environment. Standalone (no env var):
CORS middleware is added as before.

**DO NOT suggest**: "Delete the CORSMiddleware block" — that breaks standalone.

#### `ssr` (MEDIUM)

**Problem**: Frontend framework (Next.js, Nuxt, SvelteKit) needs Node.js runtime.

**Independence-preserving fix**:
```typescript
// next.config.ts — works for both modes
output: 'export',
basePath: process.env.NEXT_PUBLIC_BASE_PATH || '',
```
Build for enlace: `NEXT_PUBLIC_BASE_PATH=/s_conditions npm run build`
Standalone: `npm run dev` works at `/` as before.

**If SSR is truly needed**: enlace can't handle this today. Options:
1. Reverse proxy (Caddy proxies to a running Next.js process)
2. Mount backend only, keep frontend separate
3. Propose adding proxy mount support to enlace (future feature)

#### `missing_entry_point` (MEDIUM)

**Problem**: Entry point is in a subdirectory (e.g. `backend/main.py`).

**This is an enlace-side fix — no app code changes needed:**
```toml
# app.toml (created in app root, alongside the app code)
entry_point = "backend/main.py"
```

#### `base_path` (LOW)

**Problem**: No `basePath` / `base` configured for subpath mounting.

**Independence-preserving fix** (env-var pattern):
```typescript
basePath: process.env.NEXT_PUBLIC_BASE_PATH || ''
```
**DO NOT suggest**: `basePath: '/s_conditions'` — that breaks standalone.

#### `bare_imports` (LOW)

Works as-is under enlace. Fragile but functional. Mention it but don't
insist on changes.

#### `data_path` (LOW)

Works as-is under enlace. The relative-to-script pattern is fine. Mention
the papp convention as an option, not a requirement.

## Step 3: Present findings to the user

After running the diagnostic and interpreting results, present them as:

1. **Verdict**: "This app can/cannot be enlaced as-is"
2. **Enlace-side fixes** (no app changes): app.toml, build-time env vars
3. **App changes that preserve standalone**: env-var pattern suggestions
4. **Warnings**: anything that would break standalone if done carelessly
5. **Offer to fix**: For each issue, propose the specific code change

### Example output to user

> **s_conditions: 1 critical, 3 medium, 6 low**
>
> **Enlace-side (no app changes):**
> - Create `app.toml` with `entry_point = "backend/main.py"`
>
> **App changes (standalone preserved):**
> - `frontend/src/lib/api.ts:1` — Make API_BASE configurable:
>   `const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000/api";`
>   Build for enlace with: `NEXT_PUBLIC_API_BASE=/api/s_conditions npm run build`
> - `backend/main.py:4` — Make CORS conditional:
>   `if not os.environ.get('ENLACE_MANAGED'): app.add_middleware(...)`
>
> **Build-time (no app changes):**
> - Next.js static export: `output: 'export'` + env-var basePath
>
> **Nice to have (all work as-is):**
> - Package-style imports instead of bare imports
> - Configurable data directory
>
> Want me to apply the app changes? Standalone operation will be preserved.

## Step 4: Apply fixes (when the user agrees)

For each fix, follow this order:

### 1. Enlace-side fixes first (no app code touched)
- Create `app.toml` in app root
- Set up build scripts / .env files

### 2. App changes that preserve standalone
- Hardcoded URL → env-var with current value as default
- CORS → conditional on `ENLACE_MANAGED` env var
- basePath → env-var with empty string default

### 3. Verify standalone still works
After making changes, confirm:
- App can still run independently (e.g. `npm run dev` + `uvicorn main:app`)
- No import errors, no broken paths, no missing CORS

## Important notes

- **Never modify the app's code without user consent** — always present the
  diagnosis first and let the user decide what to change.
- **Always verify standalone operation** after suggesting changes.
- **The diagnostic tool catches common patterns but isn't exhaustive** — always
  do a manual review of the frontend's fetch calls and the backend's middleware
  stack as a sanity check.
- **Some issues are non-issues under enlace** — e.g., `uvicorn.run(port=8000)`
  in a `__main__` block is dead code (enlace imports the app directly).
- **`breaks_standalone` flag**: If an issue's `breaks_standalone` field is True,
  the suggested fix would break standalone operation. Warn the user explicitly
  and explore alternatives.
