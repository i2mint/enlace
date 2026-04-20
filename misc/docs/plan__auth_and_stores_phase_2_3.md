# Plan: Auth and User Stores (Phases 2 & 3)

**Author:** Thor Whalen & Claude
**Date:** 2026-04-19
**Status:** Plan — not yet implemented
**Depends on:** [enlace_spec.md](enlace_spec.md) §8–9, [auth_cross_cutting.md](auth_cross_cutting__Authentication_and_Authorization_as_Cross_Cutting_Concerns.md), [user_data_persistence.md](user_data_persistence__Multi_app_user_data_persistence_with_repository_abstractions.md)

---

## 1. Goals and Non-Goals

### Goals

1. Ship **shared-password, per-user, and OAuth2/OIDC** auth in a single pass, gated by per-app config. Apps stay auth-unaware.
2. Ship **per-user data stores** injected into `scope["state"]["store"]`. Apps stay storage-unaware.
3. Expose everything behind repository-pattern abstractions:
   - Python: `collections.abc.MutableMapping` (via `dol`).
   - Frontend: `zodal-store` `DataProvider` interface.
4. Default backends are **MVP-grade, no-install**:
   - Sessions, user accounts, CSRF state, per-user data: local-file stores (via `dol`).
   - Frontend: `zodal-store-fs` / `zodal-store-localstorage` as appropriate.
5. README/docs for each component list **3–5 production-grade alternatives** behind the same interface (SQLite, Redis, Postgres, S3, Supabase, etc.) — users swap backends without touching app code.
6. Diagnostics and two new skills guide agents through setup without coupling apps to enlace.

### Non-Goals

- No hot-reload of auth config (reload the gateway).
- No multi-factor auth in this pass (documented as a later IdP-delegated concern).
- No built-in user registration UI — shared-password + OAuth cover the MVP. Per-user signup is a `POST /auth/register` route that writes to the user-account `MutableMapping`; a real signup flow (email verification, password reset) is a later IdP concern.
- No enterprise IdP integration in the package (Authlib pulls in Google/GitHub; Authelia/Logto/Keycloak remain documented recommendations, not dependencies).

---

## 2. Design Principles (restated for this work)

The two CLAUDE.md principles govern everything below:

- **Apps should not need to change.** `import enlace` never appears in app code. Apps read `request.state.user_id` and `request.state.store`. That's it.
- **Enlaced apps must still work alone.** If an app wants standalone mode, `request.state.user_id` falls back to `None` / an env-var-provided dev user, and `request.state.store` falls back to a local dict or file store the app provides itself.

Plus one more, inherited from the research:

- **Repository pattern everywhere.** Every mutable piece of state (sessions, user accounts, OAuth state, CSRF tokens, per-user data) is a `MutableMapping` on the backend and a `zodal-store` provider on the frontend. Concrete backends are injected, never hardcoded.

---

## 3. Architecture Overview

```
Request
  │
  ▼
┌─────────────────────────────────────────────────┐
│ PlatformAuthMiddleware (pure ASGI)              │
│   1. Normalize path                             │
│   2. Strip client-provided identity headers     │
│   3. Resolve access level from AppConfig.access │
│   4. Apply auth check (public/shared/user/oauth)│
│   5. Set scope["state"]["user_id"], user_email  │
└─────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────┐
│ StoreInjectionMiddleware (pure ASGI)            │
│   Reads user_id → PrefixedStore(base_store,     │
│                   f"{user_id}/{app_id}/")       │
│   Sets scope["state"]["store"]                  │
└─────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────┐
│ Mounted sub-app                                 │
│   request.state.user_id  (str | None)           │
│   request.state.store    (MutableMapping)       │
│   → zero enlace imports                         │
└─────────────────────────────────────────────────┘
```

All state used by the platform itself (sessions, users, OAuth state, CSRF) lives in **one store factory** called the **platform store**. The factory produces namespaced sub-stores for each concern:

```python
platform_store["sessions"]   # session_id -> {user_id, email, created}
platform_store["users"]      # email      -> {password_hash, created}
platform_store["oauth_state"]# state_tok  -> {provider, nonce, created}
```

Per-user app data lives in a **separate** data store, also a `MutableMapping`, wrapped per request into a `PrefixedStore(base, f"{user_id}/{app_id}/")`.

Why two stores? Different lifecycles and security domains. Sessions are short-lived and platform-internal; user data is long-lived and app-visible.

---

## 4. Module Layout

All new code lives under `enlace/` — apps don't import any of it.

```
enlace/
├── auth/
│   ├── __init__.py          # public API: PlatformAuthMiddleware, helpers
│   ├── middleware.py        # PlatformAuthMiddleware (pure ASGI)
│   ├── sessions.py          # SessionStore (MutableMapping-backed)
│   ├── shared.py            # shared-password login routes
│   ├── user.py              # per-user login/register/logout routes
│   ├── oauth.py             # Authlib-based OAuth2/OIDC routes (optional import)
│   ├── csrf.py              # signed double-submit CSRF middleware
│   ├── passwords.py         # hash_password, verify_password (argon2-cffi)
│   └── cookies.py           # signed-cookie helpers (itsdangerous)
├── stores/
│   ├── __init__.py          # public API
│   ├── prefixed.py          # PrefixedStore (MutableMapping wrapper)
│   ├── mall.py              # Mall pattern
│   ├── middleware.py        # StoreInjectionMiddleware
│   ├── validation.py        # sanitize_key (path-traversal guard)
│   └── backends.py          # default file-backed MutableMapping via dol
├── base.py                  # + AccessLevel, AuthConfig additions to AppConfig
├── compose.py               # wire middleware into build_backend()
└── diagnose.py              # + auth/store-related checks
```

New optional dependencies (extras):

- `enlace[auth]`: `itsdangerous`, `argon2-cffi`
- `enlace[oauth]`: `authlib`, `httpx`
- `enlace[csrf]`: `starlette-csrf` *(or roll our own; decide during impl)*

Core install stays lean. Apps needing auth features install the extra; apps don't.

---

## 5. Configuration Model

### 5.1 Per-app access level (`app.toml` or `platform.toml`)

```toml
# apps/my_app/app.toml
[enlace]
access = "protected:user"   # public | protected:shared | protected:user
```

Or in `platform.toml`:

```toml
[apps.my_app]
access = "protected:shared"
shared_password_env = "MY_APP_SHARED_PASSWORD"  # env var holding argon2 hash
```

### 5.2 Platform-wide auth config (`platform.toml`)

```toml
[auth]
enabled = true
session_cookie_name = "enlace_session"
session_max_age_seconds = 86400
signing_key_env = "ENLACE_SIGNING_KEY"    # required if auth.enabled
secure_cookies = true                      # forces Secure=True in prod

[auth.stores]
# Default: local-file MutableMapping under ~/.enlace/platform_store/
backend = "file"
path = "~/.enlace/platform_store"
# Alternative backends documented in README; user swaps to e.g.:
# backend = "sqlite"; path = "~/.enlace/platform.db"

[auth.oauth.google]
client_id_env = "GOOGLE_CLIENT_ID"
client_secret_env = "GOOGLE_CLIENT_SECRET"
scopes = ["openid", "profile", "email"]

[stores.user_data]
backend = "file"
path = "~/.enlace/user_data"
```

Rule: **no secrets in TOML.** Always `*_env` pointers to env vars. `enlace check` validates that every referenced env var is set.

### 5.3 Pydantic additions

```python
# base.py additions (sketch)
AccessLevel = Literal["public", "protected:shared", "protected:user"]

class AppConfig(BaseModel):
    # ... existing fields ...
    access: AccessLevel = "public"
    shared_password_env: str | None = None   # required if access == protected:shared

class AuthConfig(BaseModel):
    enabled: bool = False
    session_cookie_name: str = "enlace_session"
    session_max_age_seconds: int = 86400
    signing_key_env: str = "ENLACE_SIGNING_KEY"
    secure_cookies: bool = True
    stores: StoreBackendConfig = StoreBackendConfig()
    oauth: dict[str, OAuthProviderConfig] = {}

class PlatformConfig(BaseModel):
    # ... existing fields ...
    auth: AuthConfig = AuthConfig()
    stores: dict[str, StoreBackendConfig] = {}   # e.g. {"user_data": ...}
```

---

## 6. Storage Abstraction: Python Side

### 6.1 The contract

Every storage concern in enlace uses `collections.abc.MutableMapping`. This is the repository-pattern boundary on the Python side. Concrete backends are injected via factories.

```python
# enlace/stores/backends.py
from collections.abc import MutableMapping
from typing import Callable

StoreFactory = Callable[[str], MutableMapping]
# name -> MutableMapping (e.g. factory("sessions") returns the sessions store)
```

### 6.2 MVP backend: local files via `dol`

```python
# enlace/stores/backends.py (sketch)
from dol import Files, wrap_kvs, ValueCodecs

def make_file_store_factory(root: str) -> StoreFactory:
    """Factory producing JSON-valued MutableMappings under {root}/{name}/."""
    def factory(name: str) -> MutableMapping:
        base = Files(f"{root}/{name}")
        return wrap_kvs(base, value_codec=ValueCodecs.json())
    return factory
```

No install beyond `dol` (already a soft dependency candidate — or we vendor a tiny fallback).

### 6.3 Documented alternatives (README, not code)

Every store-backend README must list:

| Backend | When to use | How to swap |
|---|---|---|
| **File** (default, MVP) | <100 users, single host, low write volume | `backend = "file"` |
| **SQLite + WAL** | single host, higher write volume, transactional safety | `backend = "sqlite"` (via `sqldol` or `sqlite3`-wrapped `MutableMapping`) |
| **Redis** | multi-process, session-heavy, TTL-native | `backend = "redis"` (via `redisdol`) |
| **Postgres** | multi-host, needs ACID + queries | `backend = "postgres"` (via `sqlalchemydol` or equivalent) |
| **S3 / R2** | user data blobs, versioning, cheap cold storage | `backend = "s3"` (via `s3dol`) |

These are **documentation**, not package code. The package ships the file backend only; others are user-implemented or pulled from the `dol` ecosystem.

### 6.4 `PrefixedStore` — the per-user scoping primitive

```python
# enlace/stores/prefixed.py
class PrefixedStore(MutableMapping):
    """Transparently prepends a prefix to every key operation."""
    def __init__(self, base: MutableMapping, prefix: str):
        self._base = base
        self._prefix = prefix
    # ... __getitem__/__setitem__/__delitem__/__iter__/__len__ as in
    #     user_data_persistence.md §"Per-user store injection"
```

Key sanitization lives in `enlace/stores/validation.py` with the exact `TenantIsolatedStore` ruleset from the research doc (block `..`, `\`, null bytes, control chars, URL-encoded variants; double-verify the prefix after construction).

### 6.5 `StoreInjectionMiddleware`

Pure ASGI. Runs after auth middleware. Reads `scope["state"]["user_id"]` and `scope["state"]["app_id"]` (set per-mount by `compose.py`), wraps `user_data_store` in `PrefixedStore(base, f"{user_id}/{app_id}/")`, attaches to `scope["state"]["store"]`.

If `user_id` is `None` (public endpoint), `scope["state"]["store"]` is `None` — app decides what to do. For standalone operation, the app can provide its own dict fallback.

---

## 7. Storage Abstraction: Frontend Side

The frontend equivalent is [`zodal-store`](/Users/thorwhalen/Dropbox/py/proj/i/_zodals/). It provides a `DataProvider` interface with swappable backends: `zodal-store-fs`, `zodal-store-localstorage`, `zodal-store-s3`, `zodal-store-supabase`.

enlace does **not** ship frontend code. The `enlace-user-stores` skill (§10) instructs agents to:

- Use `zodal-store-localstorage` (browser-side cache, default MVP).
- Use a thin HTTP provider that calls the enlaced backend's `/api/{app_id}/store/{key}` endpoints (platform convention).
- Document swap paths to `zodal-store-s3`, `zodal-store-supabase` as the app grows.

The HTTP provider is trivial — ~30 lines wrapping `fetch()` with the `DataProvider` interface. Skill ships the template.

---

## 8. Auth Implementation Details

### 8.1 `PlatformAuthMiddleware`

Verbatim adapted from [auth_cross_cutting.md lines 62–168](auth_cross_cutting__Authentication_and_Authorization_as_Cross_Cutting_Concerns.md), with these enlace-specific adjustments:

- Reads access levels from `PlatformConfig.apps[*].access`, not a hand-built dict. Longest-prefix match on the mount route prefix.
- Strips **all** inbound identity headers (`X-User-ID`, `X-User-Email`, `X-Forwarded-User`, etc.) from `scope["headers"]` before any downstream code runs. This is a security invariant — don't let clients spoof identity.
- Default for unmatched paths: `protected:user` (deny-by-default).
- Path normalization is the first operation (per CVE lessons in the research doc).

### 8.2 Shared-password flow

- Password stored as **argon2id hash** in an env var pointed to by `shared_password_env`.
- `POST /auth/shared-login` validates submitted password against hash via `argon2.PasswordHasher().verify()` (constant-time, salt-aware — simpler and safer than manual `hmac.compare_digest` against a hash).
- Sets signed cookie (`shared_auth_{app_id}`) scoped to the app's route prefix.
- `POST /auth/logout` clears it.

### 8.3 Per-user flow

- `POST /auth/register` — stores `{email, argon2_hash, created_at}` in `platform_store["users"]`.
- `POST /auth/login` — verifies, creates session in `platform_store["sessions"]`, sets `enlace_session` cookie with the session ID.
- `POST /auth/logout` — deletes session, clears cookie.
- Session cookie: `HttpOnly=True`, `Secure=True` (prod), `SameSite=Lax`, signed with `ENLACE_SIGNING_KEY`.

### 8.4 OAuth2/OIDC flow

- Authlib is imported lazily — `enlace[oauth]` extra.
- Platform-level routes: `/auth/login/{provider}` and `/auth/callback`.
- On callback: create local session in `platform_store["sessions"]`, set cookie. The OAuth tokens themselves are discarded — we're using OAuth for identity, not resource access. (Token storage for API access is a later concern.)
- Providers configured in `platform.toml` under `[auth.oauth.{name}]`. MVP supports Google and GitHub; adding more is a config change.

### 8.5 CSRF

Signed double-submit cookie, applied only on state-changing routes (`POST`/`PUT`/`PATCH`/`DELETE`). Exempt paths: `/auth/login/{provider}` (OAuth redirects need to work cross-site). Implementation: roll our own thin pure-ASGI middleware in `enlace/auth/csrf.py` — `starlette-csrf` is an option but the logic is ~60 lines and the dependency cost isn't worth it.

### 8.6 WebSocket auth

Same middleware handles `scope["type"] == "websocket"`. Reads cookie from handshake headers, validates, sets `scope["state"]["user_id"]`. Origin header validation against `platform.toml`'s `allowed_origins`. On rejection: `{"type": "websocket.close", "code": 1008}`.

### 8.7 CLI additions

```bash
enlace auth init                    # scaffold [auth] section in platform.toml
enlace auth hash-password           # argon2id hash a shared password (interactive)
enlace auth generate-signing-key    # print a urlsafe 32-byte secret
enlace auth list-sessions           # inspect platform_store["sessions"]
enlace auth revoke-session <id>     # delete a session
```

None of these touch app code.

---

## 9. Diagnostic Additions (`enlace/diagnose.py`)

New `Category` enum entries and checks:

| Check | Severity | What it catches | Fix suggestion (standalone-preserving) |
|---|---|---|---|
| `SUBAPP_AUTH_MIDDLEWARE` | HIGH | Sub-app adds its own auth middleware (e.g. `AuthenticationMiddleware`) | Wrap in `if not os.environ.get('ENLACE_MANAGED'): ...` |
| `CLIENT_IDENTITY_HEADER` | HIGH | App reads `X-User-ID` / `X-Forwarded-User` from request headers | Use `request.state.user_id` with env-var fallback |
| `HARDCODED_USER_ID` | MEDIUM | Literal user IDs in source (e.g. `"admin"`, `"user_1"`) | Read from `request.state.user_id`; `os.environ['DEV_USER']` for standalone |
| `SESSION_COOKIE_IN_SUBAPP` | MEDIUM | Sub-app sets its own session cookie | Delegate to platform; conditional on `ENLACE_MANAGED` |
| `STORE_IMPORT_IN_APP` | HIGH | App does `import enlace` or imports from an enlace storage module | Use `request.state.store` with a dict fallback for standalone |
| `UNSAFE_KEY_IN_STORE` | HIGH | App passes user-supplied input directly as storage key | Validate via the sanitization rules before `store[key]` |
| `MISSING_SIGNING_KEY` | HIGH (platform) | `auth.enabled=true` but `ENLACE_SIGNING_KEY` unset | `enlace auth generate-signing-key` |

Each check has a `docstring_with_fix` following the existing `diagnose.py` convention. Severity `HIGH` blocks `enlace check` in strict mode; `MEDIUM` warns.

---

## 10. Skills (`.claude/skills/`)

Two new skills, both produce code/config that runs standalone AND under enlace.

### 10.1 `enlace-auth-setup`

**Triggers:** "add auth to my platform", "protect this app", "set up login", "add Google sign-in", "require a password for X".

**Workflow the skill walks the agent through:**
1. Ask per-app: `public` / `protected:shared` / `protected:user`.
2. Generate signing key (`enlace auth generate-signing-key`) → add to `.env`.
3. For `protected:shared`: prompt for password → hash it → add hash to `.env` → add `shared_password_env` to app config.
4. For per-user: ensure `[auth]` block in `platform.toml`, confirm `platform_store` path.
5. For OAuth: walk through creating OAuth app in Google/GitHub console → populate `client_id_env`/`client_secret_env`.
6. Run `enlace check` to confirm config is valid.
7. Run `enlace diagnose apps/*` to confirm no sub-app is stomping on auth.

**Critical guardrails (stops the agent from inventing its own middleware):**
- Never add `AuthenticationMiddleware` or `BaseHTTPMiddleware` to app code.
- Never set session cookies from app code.
- Never read `X-User-ID` from headers — always `request.state.user_id`.
- If an app already has auth, propose the env-var-with-default retrofit before ripping it out.

### 10.2 `enlace-user-stores`

**Triggers:** "give this app per-user storage", "add a user-scoped data store", "persist user settings", "store per-user X".

**Workflow:**
1. Confirm the app's `access` level is `protected:user` (stores without identity are meaningless).
2. Ensure `[stores.user_data]` block in `platform.toml`; default to file backend under `~/.enlace/user_data`.
3. In app code, replace any storage access with `request.state.store`. Fallback for standalone: `request.state.store if hasattr(request.state, 'store') else {}` or a small file-backed store the app provides.
4. Frontend: scaffold a `zodal-store` HTTP provider that points at `/api/{app_id}/store`. Backend routes are generated by a platform helper (not hand-rolled per app).
5. Document the production swap path — `# To upgrade to SQLite/Redis/Postgres, change [stores.user_data].backend`.
6. Run `enlace diagnose apps/{app}` to catch hardcoded paths, unsafe keys, enlace imports.

**Critical guardrails:**
- Never `import enlace` in app code.
- Never construct `user_id` from request bodies or query params.
- Always sanitize user-supplied keys before `store[key]`.
- Large blobs (>1 MB) → a separate `BlobStore` (presigned URL pattern from the research doc); not the KV store.

### 10.3 Skill integration

Both skills live in `.claude/skills/` at the enlace repo root, so they're discoverable when working on enlace or on any app mounted under it. They invoke existing tools:

- `enlace check`, `enlace auth *` (CLI commands added in §8.7)
- `enlace diagnose` (extended per §9)
- `zodal-store-*` npm packages (documented, not bundled)

---

## 11. Testing Strategy

Unit tests per module (`enlace/tests/`):

- `test_auth_middleware.py` — path normalization bypass suite (`//`, `%2e%2e`, `\`, null bytes, mixed case); deny-by-default; header stripping; identity injection.
- `test_sessions.py` — SessionStore roundtrip over a dict backend and a tmp-file backend.
- `test_passwords.py` — hash/verify roundtrip; rejection of tampered hashes.
- `test_oauth.py` — mocked Authlib flow (callback → session creation).
- `test_csrf.py` — double-submit accept/reject, exempt paths.
- `test_prefixed_store.py` — key sanitization attacks; prefix isolation; `PrefixedStore` composition.
- `test_store_middleware.py` — injection under asgi mount; `user_id=None` path.

Integration tests (`tests/`):

- `test_auth_e2e.py` — full login → protected endpoint → logout, per access level, against a live uvicorn + mounted apps.
- `test_standalone_preservation.py` — every example app in `apps/` starts and serves its happy path with `ENLACE_MANAGED` **unset** (proves the env-var-with-default pattern works).
- `test_oauth_e2e.py` — uses Authlib's test utilities or a mocked provider.

---

## 12. Phasing

**Phase 2a — Auth foundation (ships together):**
- `AccessLevel` + `AuthConfig` on Pydantic models
- File-backed `MutableMapping` factory
- `PlatformAuthMiddleware` + path normalization + header stripping
- Shared-password + per-user flows
- CSRF middleware
- `enlace auth *` CLI
- Diagnostics: `SUBAPP_AUTH_MIDDLEWARE`, `CLIENT_IDENTITY_HEADER`, `MISSING_SIGNING_KEY`
- Skill: `enlace-auth-setup`

**Phase 2b — OAuth:**
- Authlib integration behind `enlace[oauth]` extra
- Google + GitHub providers
- Tests against mocked provider

**Phase 3 — Stores:**
- `PrefixedStore`, `Mall`, `StoreInjectionMiddleware`, key validation
- Platform-provided `/api/{app_id}/store/{key}` routes for frontend use
- Diagnostics: `STORE_IMPORT_IN_APP`, `UNSAFE_KEY_IN_STORE`, `HARDCODED_USER_ID`
- Skill: `enlace-user-stores`

Phase 2a and 2b can ship in the same release if OAuth lands on time; Phase 3 follows after Phase 2a is proven on a real app.

---

## 13. Open Decisions

1. **CSRF library or hand-rolled?** Default: hand-rolled (~60 lines, no dep). Revisit if it grows.
2. **`dol` as a hard dep?** Currently a soft convention. If we want the file backend to ship with the package, `dol` becomes a hard dep for `enlace[auth]`. Acceptable — it's lightweight.
3. **Session store separate from user-data store?** Plan says yes (separate factories, same interface). Alternative: one `Mall` with conventional top-level keys. Separate is simpler to reason about and to swap independently (e.g. sessions in Redis, user data in Postgres).
4. **Register route — include CAPTCHA / rate limit?** Out of scope for MVP. Documented as a later concern. Rate limiting belongs at the gateway (Caddy) anyway.
5. **Skill packaging:** ship the two skills in the enlace repo's `.claude/skills/`, or distribute as installable skills (`skill-enable` pattern)? Default: in-repo for now; revisit when we see adoption.

---

## 14. What "Done" Looks Like

After Phase 2 and Phase 3:

- Any app can declare `access = "protected:user"` in `app.toml` and get full auth with zero code changes.
- `request.state.user_id` and `request.state.store` are the only two things apps need to know about — and both fall back gracefully when the app runs standalone.
- An agent can set up auth and per-user storage for a new app in one guided skill run, without writing middleware.
- The diagnostic catches every pattern that would couple an app to enlace or break standalone operation.
- Default backends work with zero install. The README lists five production alternatives with a one-line config change to swap.
