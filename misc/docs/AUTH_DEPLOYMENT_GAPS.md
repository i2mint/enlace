# Auth Deployment Gaps

**Status:** auth is implemented in enlace (middleware, routes, stores) and
configured on `/opt/tw_platform` (platform.toml, .env hashes, per-app access,
seeded thor user), but **`auth.enabled` is set to `false`** because enabling
it on a real deployment surfaces these gaps. Close them, then flip to `true`.

Discovered during first live enable on 2026-04-20 (tw_platform /
`apps.thorwhalen.com`). Rollback path on that deployment: `git reset --hard
956a2dc && systemctl restart enlace-backend`. Auth-enabled commit for
reference: `84138b5`.

## Gap 1 — Frontend paths fall through deny-by-default (blocker)

`PlatformAuthMiddleware` builds `AccessRule`s from `app.route_prefix`, which is
`/api/{app_name}`. Frontend assets are served at `/{app_name}/` (no `/api/`
prefix), and the platform index is `/`. None of these match any rule, so
`_longest_prefix(...) → None → level = "protected:user"` (deny by default).
Result: with auth on, **the home page, the app index, and every app frontend
return 401**, even for apps declared `access = "public"`.

### Repro
```python
from fastapi.testclient import TestClient
from enlace.compose import create_app
c = TestClient(create_app(), base_url="https://testserver")
# Assume platform.toml has [auth] enabled=true and one app with access="public"
c.get("/chord_renderer/").status_code  # 401, expected 200
c.get("/").status_code                  # 401, expected 200
```

### Fix options

1. **Extend access rules to cover both prefixes.** When building `access_rules`
   in `_wire_auth_and_stores`, add a second rule per app keyed on
   `/{app.name}/` (or `app.frontend_prefix` if we introduce one) with the same
   level. Cleanest.
2. **Only apply auth to the `/api/*` space.** Add a global exempt-prefix list
   (default `("/",)` for the index and `/{app}/` for frontend paths) that
   short-circuits before the deny-by-default clause. Easier but leans on
   the apps to implement their own gating client-side.
3. **Change deny-by-default to allow-by-default.** Simplest but violates the
   security posture laid out in `auth_cross_cutting__*.md`. Don't.

Prefer (1). Design doc already calls out path-based access levels; this is
just making the path set complete.

## Gap 2 — No login UI (blocker)

`make_auth_router` exposes only JSON POST endpoints: `/auth/register`,
`/auth/login`, `/auth/logout`, `/auth/shared-login`, `GET /auth/whoami`.
A browser visiting `/auth/login` with no frontend app gets nothing to fill
out. On mobile with no bookmarklet, the user cannot authenticate.

### Fix options

1. **Ship a default login page** inside enlace (`/auth/login` returns HTML on
   `Accept: text/html`, JSON on `Accept: application/json` — same endpoint,
   content-negotiated). Minimal form: email + password → fetch → redirect to
   `?next=` or `/`. Also needed: `/auth/shared-login?app=X` HTML variant for
   shared mode. This is the standard pattern and the right default.
2. **Document that each app's frontend must implement a login form.** Works
   for apps that have a proper frontend; breaks for frontend-only or
   function-collection apps. Don't rely on this alone.

Prefer (1). Keep it un-styled / minimal-css so apps can override.

## Gap 3 — CSRF token exposure (UX paper cut)

`CSRFMiddleware` verifies `X-CSRF-Token` header against the *unsigned* value
inside the `enlace_csrf` cookie. But the cookie the client receives only has
the signed form, and the client doesn't have the signing key to unwrap it.
Current workaround: extract the unsigned value server-side before shipping
the cookie — which a frontend cannot do.

### Fix options

1. **`GET /auth/csrf`** returns `{"csrf": "<unsigned>"}` and (re-)sets the
   `enlace_csrf` cookie. Frontends call it once, stash the value in memory,
   send it on every mutating request.
2. **Inject a `<meta name="csrf-token" content="...">`** into any HTML
   response passing through the middleware. More magic, but zero-effort for
   frontends.
3. **Match on the signed value both ways.** Ship the signed cookie value as
   the header value too (don't unsign server-side). Simpler but leaks the
   "double submit" invariant that the two are independently known.

Prefer (1). It composes cleanly with (1) from Gap 2: the login page reads
CSRF from `/auth/csrf` before submitting.

## Gap 4 — Traefik rule on apex domain (deployment-only, minor)

For the tw_platform deployment, `/data/coolify/proxy/dynamic/thorwhalen.yaml`
routes `thorwhalen.com`'s `/api/*` and `/{app}/` paths to enlace but leaves
`/auth/*` going to the static landing (Caddy). `apps.thorwhalen.com` is a
catch-all to enlace and works fine for auth. Either:

1. Add `/auth/` to `thorwhalen-apps` (or make a new `thorwhalen-auth`
   router) so login works from the apex domain too, or
2. Standardize on `apps.thorwhalen.com` as the authenticated hostname and
   document that the apex is landing-only.

This is a deployment-specific fix, not an enlace change.

## Acceptance criteria to flip `[auth] enabled` back to `true`

- [ ] Gap 1: a public app's frontend returns 200 when `enabled=true`.
- [ ] Gap 1: the index `/` returns 200 when `enabled=true`.
- [ ] Gap 2: browser visit to `/auth/login` renders a login form and
      successfully sets a session cookie end-to-end.
- [ ] Gap 3: a fetch()-based login flow works without server-side CSRF
      extraction.
- [ ] Integration test: TestClient login + GET a protected app's endpoint
      → 200; logout → 401.
