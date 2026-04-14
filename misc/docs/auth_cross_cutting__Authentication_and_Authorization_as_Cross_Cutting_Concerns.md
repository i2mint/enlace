# Authentication and Authorization as Cross-Cutting Concerns

**Author:** Thor Whalen
**Date:** April 8, 2026

Every major software architecture tradition — Clean, Hexagonal, Onion, DDD — agrees on one thing: **authentication belongs outside business logic** [1]. This report lays out how to enforce that principle in a modular Python app platform where multiple independent FastAPI/ASGI backends are mounted under a single domain, and individual apps have zero awareness of auth. The core mechanism is a pure ASGI middleware that inspects a configuration registry, applies the correct auth check per path prefix, and injects user identity into the ASGI scope — exactly mirroring how production API gateways like Kong, Traefik, and Envoy handle auth before backend services ever see a request [2][3][4].

The report covers 14 research questions across four domains: gateway/middleware patterns, per-user authentication systems, security concerns, and clean architecture integration. It separates "what to build now" (shared-password gates via signed session cookies) from "what to plan for later" (per-user accounts, OAuth2/OIDC, external identity providers).

---

## Part I — The gateway pattern inside a single process

### How API gateways enforce auth before backends see anything

All production API gateways follow the same pattern: authentication runs as a filter/plugin/middleware *before* the request reaches the backend. If auth fails, the gateway returns 401/403 directly. If it succeeds, the gateway enriches the request with identity information and forwards it downstream [2][3][4][5].

**Kong** uses access-phase plugins that append headers like `X-Consumer-ID`, `X-Consumer-Username`, and `X-Credential-Identifier` before proxying to upstream services [2]. **Traefik** delegates to an external auth server via ForwardAuth — if the auth server returns 2xx, Traefik copies specified response headers (e.g., `X-Forwarded-User`, `X-Auth-Request-Email`) onto the forwarded request [3]. **Envoy** uses `ext_authz` filters that call an external gRPC/HTTP service and inject `allowed_upstream_headers` like `x-user-id` and `x-user-roles` into the upstream request [4]. **AWS API Gateway** uses Lambda authorizers that return IAM policies plus context key-value pairs mappable to request headers [5].

The standard header conventions across gateways are: `X-User-ID` or `x-user-id` for the user identifier, `X-Forwarded-User` for the username/email, `Authorization: Bearer <JWT>` for token passthrough, and `X-User-Roles` for authorization data. **A critical security rule applies universally: gateways must strip all incoming identity headers from the client before setting them from the auth result** — otherwise clients can spoof identity headers [6].

This maps directly to the FastAPI mounted sub-apps architecture. The parent Starlette app is the gateway, ASGI middleware is the auth plugin, and mounted sub-apps are the backend services. But within a single process there is an important advantage: **identity can travel via the ASGI `scope` dict rather than HTTP headers**, which is more efficient and tamper-proof [7].

### Pure ASGI middleware — the only correct approach

Starlette's built-in `AuthenticationMiddleware` is itself a pure ASGI middleware (not based on `BaseHTTPMiddleware`) that sets `scope["auth"]` and `scope["user"]` for all downstream code [8][9]. Its `AuthenticationBackend` interface has a single method — `authenticate(conn)` — that returns `None` for unauthenticated requests (which proceed with `UnauthenticatedUser`), an `(AuthCredentials, BaseUser)` tuple for authenticated requests, or raises `AuthenticationError` to reject [8].

Parent Starlette middleware **does propagate to all mounted sub-apps**. The middleware stack wraps the entire Router, which includes all Mount entries. The execution flow is: `ServerErrorMiddleware → YourAuthMiddleware → ExceptionMiddleware → Router → Mount("/app1") → sub_app_1` [9][10]. The `scope` dict — including `scope["user"]` and `scope["auth"]` — passes through the entire chain.

**`BaseHTTPMiddleware` must never be used for auth.** Starlette maintainers have documented terminal bugs: ContextVar propagation breaks across middleware layers, exceptions from mounted sub-apps can be silently swallowed, and background tasks execute synchronously within the middleware. One maintainer commented: "I actually think the problems with BaseHTTPMiddleware are terminal: it's not fixable" [11][12]. Deprecation for Starlette 1.0 is under discussion [12].

The pure ASGI middleware interface follows the three-callable pattern specified in the ASGI 3.0 spec [7]:

```python
class AuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Auth logic: inspect scope["headers"], extract cookies/tokens
        # On success: enrich scope and forward
        scope["user"] = authenticated_user
        await self.app(scope, receive, send)

        # On failure: send error response directly
        # response = JSONResponse({"error": "Unauthorized"}, status_code=401)
        # await response(scope, receive, send)
```

### Path-dispatching middleware maps access levels to sub-apps

The recommended pattern for applying different auth requirements to different mounted sub-apps is a **single parent middleware that checks `scope["path"]` against the platform's app registry** [10]. This directly mirrors Traefik's per-IngressRoute middleware selection and Envoy's `ExtAuthzPerRoute` configuration [3][4].

While Starlette also supports per-Mount middleware via `Mount("/path", app=sub_app, middleware=[...])` since PR #1649 [10], this approach has a caveat: mount-level middleware is not wrapped in `ExceptionMiddleware`, so unhandled errors won't trigger custom error handlers.

Here is the complete platform auth middleware with path-based dispatching:

```python
import hmac
import hashlib
import json
import time
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, RedirectResponse

class PlatformAuthMiddleware:
    """Pure ASGI middleware — dispatches auth by path prefix against config registry.
    
    ⚠️ SECURITY: Defaults to most restrictive access level for unmatched paths.
    """
    def __init__(self, app, auth_config: dict, session_backend):
        self.app = app
        self.auth_config = auth_config        # {"/api/myapp": "protected:user", ...}
        self.session_backend = session_backend  # validates session cookies

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # ── Step 1: Normalize path BEFORE auth check ──
        path = self._normalize_path(scope["path"])
        scope["path"] = path  # ensure downstream sees normalized path

        # ── Step 2: Resolve access level from config ──
        access_level = self._resolve_access_level(path)

        # ── Step 3: Apply auth based on access level ──
        if access_level == "public":
            user = await self._try_authenticate(scope)
            scope["user"] = user or UnauthenticatedUser()
            await self.app(scope, receive, send)
            return

        if access_level == "protected:shared":
            if not await self._check_shared_session(scope):
                await self._send_unauthorized(scope, receive, send)
                return
            scope["user"] = SharedUser()
            await self.app(scope, receive, send)
            return

        if access_level == "protected:user":
            user = await self._try_authenticate(scope)
            if not user or not user.is_authenticated:
                await self._send_unauthorized(scope, receive, send)
                return
            scope["user"] = user
            # Inject into scope["state"] for sub-app access
            scope.setdefault("state", {})
            scope["state"]["user_id"] = user.identity
            scope["state"]["user_email"] = user.email
            await self.app(scope, receive, send)
            return

        # Default: reject (deny-by-default)
        await self._send_unauthorized(scope, receive, send)

    def _resolve_access_level(self, path: str) -> str:
        """Longest-prefix match against config. Defaults to most restrictive."""
        for prefix in sorted(self.auth_config, key=len, reverse=True):
            if path.startswith(prefix):
                return self.auth_config[prefix]
        return "protected:user"  # ⚠️ DENY BY DEFAULT

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalize path to prevent auth bypass via path manipulation.
        
        ⚠️ CRITICAL: This prevents //admin, /../admin, /%2e%2e/admin bypasses.
        """
        import posixpath
        import re
        from urllib.parse import unquote
        path = unquote(path)
        path = re.sub(r'/+', '/', path)  # collapse multiple slashes
        path = posixpath.normpath(path)   # resolve .. and .
        if not path.startswith('/'):
            path = '/' + path
        return path

    async def _try_authenticate(self, scope):
        """Extract and validate session cookie. Returns user or None."""
        conn = HTTPConnection(scope)
        session_id = conn.cookies.get("session")
        if not session_id:
            return None
        return await self.session_backend.get_user(session_id)

    async def _check_shared_session(self, scope):
        """Check for valid shared-password session cookie."""
        conn = HTTPConnection(scope)
        return conn.cookies.get("shared_auth") is not None  # signed cookie

    async def _send_unauthorized(self, scope, receive, send):
        """Return 401 for API requests, redirect for browser requests."""
        conn = HTTPConnection(scope)
        accept = dict(scope.get("headers", [])).get(b"accept", b"").decode()
        if "text/html" in accept:
            response = RedirectResponse("/auth/login", status_code=303)
        else:
            response = JSONResponse({"error": "Authentication required"}, 401)
        await response(scope, receive, send)
```

---

## Part II — Authentication mechanisms by access level

### Shared-password gates: login form plus signed cookie

For `protected:shared` apps, a single shared password protects access for a small group. **HTTP Basic Auth is inferior to a login form with a signed session cookie** for web-facing apps: Basic Auth offers no logout mechanism (the browser caches credentials until closed), renders an ugly native dialog, and sends base64-encoded credentials with every request [13][14].

The correct implementation uses a login form that sets a **signed session cookie** after password verification. Security requirements from OWASP [13][14][15]:

- **Hash the shared password** even though it's a single secret. Store an Argon2id hash (preferred) or bcrypt hash (work factor ≥ 10) in config, never plaintext [15].
- **Use `hmac.compare_digest()`** for all password/hash comparisons to prevent timing side-channels.
- **Set cookie attributes**: `HttpOnly=True` (prevents JS access), `Secure=True` (HTTPS only), `SameSite=Lax` (CSRF mitigation), `Max-Age` for expiration [14].
- **HTTPS is mandatory** — use `HTTPSRedirectMiddleware` in production [14].

```python
import hmac
import hashlib
import secrets
import time
from itsdangerous import URLSafeTimedSerializer

# ── Shared-password login endpoint (platform-level) ──
SERIALIZER = URLSafeTimedSerializer(secret_key="platform-secret-key-change-me")

async def shared_login(scope, receive, send):
    """POST /auth/shared-login — validates shared password, sets signed cookie."""
    body = await _read_body(receive)
    submitted_password = parse_form(body).get("password", "")
    app_path = parse_form(body).get("app_path", "/")

    # Load the hashed password for this app from config
    expected_hash = get_shared_password_hash(app_path)

    # ⚠️ Use constant-time comparison against the hash
    password_hash = hashlib.sha256(submitted_password.encode()).hexdigest()
    if not hmac.compare_digest(password_hash, expected_hash):
        response = JSONResponse({"error": "Invalid password"}, 403)
        await response(scope, receive, send)
        return

    # Create signed token with expiration
    token = SERIALIZER.dumps({"app": app_path, "ts": time.time()})
    response = RedirectResponse(app_path, status_code=303)
    response.set_cookie(
        "shared_auth", token,
        httponly=True, secure=True, samesite="lax",
        max_age=86400,  # 24 hours
        path=app_path,  # scope cookie to specific app
    )
    await response(scope, receive, send)
```

### Per-user sessions: cookie-based is the right choice for this platform

For `protected:user` apps, the choice between cookie-based sessions and JWT tokens is settled for a single-domain multi-app platform. **Auth0's own documentation states:** "When the SPA calls only an API that is served from a domain that can share cookies with the domain of the SPA, no tokens are needed. OAuth adds additional attack vectors without providing any additional value" [16].

JWTs stored in `localStorage` are **strongly discouraged by OWASP** because any XSS vulnerability grants full credential theft [17]. JWTs in `HttpOnly` cookies are functionally "session cookies with extra steps and no advantages" — they add token size overhead (300+ bytes vs. 32-byte session IDs), require refresh token complexity, and cannot be immediately revoked without server-side state anyway [18].

**Cookie-based sessions win** for this platform because cookies set on `platform.example.com` are automatically sent for all sub-paths (`/app1`, `/app2`) with zero configuration. `HttpOnly` prevents XSS exfiltration, and `SameSite=Lax` mitigates CSRF for JSON APIs [14][19].

```python
import secrets
import time
from collections.abc import MutableMapping

class SessionStore:
    """Server-side session storage using MutableMapping (dol-compatible).
    
    Session data lives server-side; only the session ID travels in the cookie.
    """
    def __init__(self, store: MutableMapping, max_age: int = 86400):
        self.store = store    # Any MutableMapping: dict, SQLite-backed, Redis, etc.
        self.max_age = max_age

    def create(self, user_id: str, email: str) -> str:
        session_id = secrets.token_urlsafe(32)
        self.store[session_id] = {
            "user_id": user_id,
            "email": email,
            "created": time.time(),
        }
        return session_id

    async def get_user(self, session_id: str):
        data = self.store.get(session_id)
        if not data:
            return None
        if time.time() - data["created"] > self.max_age:
            del self.store[session_id]
            return None
        return AuthenticatedUser(
            identity=data["user_id"],
            email=data["email"],
        )

    def destroy(self, session_id: str):
        self.store.pop(session_id, None)
```

### The FastAPI auth library landscape in 2026

The library ecosystem divides cleanly into **middleware-compatible** and **dependency-injection-only** tools. For a platform where apps must have zero auth awareness, only middleware-compatible libraries qualify.

**Starlette's built-in `AuthenticationMiddleware`** [8] is the foundation — it calls a custom `AuthenticationBackend.authenticate()` on every request, sets `request.user` and `request.auth`, and works across mount boundaries. **This is the canonical choice.** Libraries like **fastapi-auth-middleware** (~300 GitHub stars) [20] provide convenience wrappers, and **imia** (~100 stars) [21] adds pluggable authenticators (API key, session, basic auth, token) with a user-provider pattern.

**fastapi-users** (~4,800 stars) is in **maintenance mode as of 2025** — security patches only — and requires `Depends(current_active_user)` in app code, making it incompatible with the zero-awareness pattern [22]. **python-jose is effectively abandoned** with known vulnerabilities; FastAPI officially recommends **PyJWT** (~5,000 stars, 2.7M weekly PyPI downloads) as the JWT library [22].

**Authlib** (~4,600 stars, actively maintained) is the de facto standard for OAuth2/OIDC client integration with Starlette [23]. Its Starlette integration stores OAuth state in `request.session` (via `SessionMiddleware`), which fits perfectly with a cookie-session platform.

### Planning for OAuth2/OIDC: Authlib as the platform adapter

When "Sign in with Google/GitHub" is needed later, Authlib handles the entire Authorization Code flow as a platform-level concern [23][24]. The flow: user clicks "Login with Google" → platform redirects to Google → user authenticates → Google redirects back with `code` → platform exchanges code for tokens server-side → platform creates a local session cookie → apps see `request.state.user_id` with zero OAuth awareness.

```python
from authlib.integrations.starlette_client import OAuth

oauth = OAuth()
oauth.register('google',
    client_id='...',
    client_secret='...',
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid profile email'},
)

# Platform-level route — apps never see this
@platform_router.get('/auth/login/google')
async def login_google(request: Request):
    redirect_uri = request.url_for('auth_callback')
    return await oauth.google.authorize_redirect(request, redirect_uri)

@platform_router.get('/auth/callback')
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get('userinfo')
    # Create platform session — app never knows OAuth happened
    session_id = session_store.create(
        user_id=userinfo['sub'], email=userinfo['email']
    )
    response = RedirectResponse('/')
    response.set_cookie("session", session_id,
        httponly=True, secure=True, samesite="lax")
    return response
```

External identity providers (Auth0, Logto, Keycloak, ZITADEL) all expose standard OIDC endpoints and work with Authlib via `server_metadata_url` [24][25]. ZITADEL provides an official FastAPI + Authlib example [25].

### Self-hosted identity providers: a graded recommendation

For a single-developer platform, the simplest path is **no separate IdP** — just Authlib + a users table in SQLite with Argon2id-hashed passwords. When a separate IdP becomes necessary (SSO across independent services, MFA, social login management), the options scale from ultralight to enterprise:

**Pocket ID** [26] (Go + Svelte, ~6,000 stars) is passkey-only authentication in a single container with SQLite — the lightest option at under 30MB RAM. **Authelia** [27] (~23,000 stars, Go) is purpose-built for reverse-proxy forward auth with YAML configuration, now OIDC Certified, consuming under 30MB RAM. **Logto** [28] (~19,000 stars, Node.js) provides a full modern IdP with beautiful UI and SDKs for many frameworks. **ZITADEL** [29] (~10,000 stars, Go) offers event-sourced multi-tenancy with API-first design at ~100MB RAM. **Authentik** (~20,000 stars, Python/Django) adds a visual flow builder for complex auth flows. **Keycloak** (~25,000 stars, Java) is the industry standard but demands 512MB–1GB+ RAM and significant expertise [27].

The recommendation ladder: start with no IdP (Authlib + SQLite), move to **Authelia** or **Pocket ID** for simple gating, then **Logto** or **ZITADEL** for full-featured identity management [27].

---

## Part III — Security: CSRF, WebSockets, and middleware bypass risks

### CSRF protection for cookie-authenticated SPAs

CSRF matters because cookie-based auth means the browser automatically attaches credentials to cross-site requests. OWASP's primary recommendation is **token-based CSRF defense**, specifically the **Signed Double-Submit Cookie** pattern for SPAs [19]:

1. Server generates a CSRF token and sets it in a non-`HttpOnly` cookie (`XSRF-TOKEN`) with `SameSite=Lax`
2. SPA reads the token from the cookie via JavaScript
3. SPA attaches the token to a custom header (`X-XSRF-TOKEN`) on state-changing requests
4. Server validates the header matches the cookie, using HMAC to bind the token to the session

**`SameSite=Lax` alone is not sufficient** — it permits cookies on cross-site top-level GET navigations, leaves subdomain cookie-injection attacks open, and Chrome has edge-case exemptions [30][31]. OWASP explicitly states: "This attribute should not replace a CSRF Token. Instead, it should co-exist with that token" [19].

For this platform, the practical defense stack is: `SameSite=Lax` session cookies + **signed double-submit CSRF cookies** + Origin header validation as defense-in-depth. The `starlette-csrf` library [32] implements this as pure ASGI middleware with configurable `required_urls` and `exempt_urls`.

⚠️ **Don't do this:** Assume JSON-only APIs are immune to CSRF. While `Content-Type: application/json` triggers CORS preflights from cross-origin, same-origin pages compromised by XSS bypass this entirely. XSS defeats all CSRF mitigations [19].

### WebSocket auth happens during the handshake

The browser WebSocket API does not support custom HTTP headers during the handshake [33]. However, **browsers do automatically send cookies** with the WebSocket upgrade request. For a cookie-session platform, this means WebSocket auth works identically to HTTP auth — the platform's ASGI middleware reads the session cookie from `scope["headers"]` during the handshake.

Starlette's `AuthenticationMiddleware` **explicitly handles WebSocket connections** — its `authenticate(conn)` method receives an `HTTPConnection` wrapping the ASGI scope for both HTTP and WebSocket types. On `AuthenticationError`, it sends `{"type": "websocket.close", "code": 1000}` [8][9]. After authentication, `websocket.user` and `websocket.auth` are available for the connection's lifetime via the persisted `scope`.

The critical security concern is **Cross-Site WebSocket Hijacking (CSWSH)** — essentially CSRF for WebSockets, but worse because it enables bidirectional communication. A malicious page at `evil.com` can open `new WebSocket("wss://yourapp.com/ws")` and the browser sends session cookies automatically [34][35]. **Always validate the Origin header** during the WebSocket handshake against an explicit allowlist:

```python
async def __call__(self, scope, receive, send):
    if scope["type"] == "websocket":
        origin = self._get_header(scope, b"origin")
        if origin and origin not in ALLOWED_ORIGINS:
            # Reject: send websocket.close before accepting
            await send({"type": "websocket.close", "code": 1008})
            return
        # Authenticate via session cookie (same as HTTP)
        user = await self._try_authenticate(scope)
        if not user:
            await send({"type": "websocket.close", "code": 1008})
            return
        scope["user"] = user
    await self.app(scope, receive, send)
```

For long-lived WebSocket connections, implement **periodic session revalidation** (every 30 minutes per OWASP guidance) and close connections when sessions expire or users log out [33].

### Middleware-only auth: real bypass CVEs and how to prevent them

Relying solely on middleware creates a **single point of failure** that violates defense-in-depth [36]. Recent CVEs demonstrate this is not theoretical:

**CVE-2025-29927** (CVSS 9.1) — Next.js used an internal header `x-middleware-subrequest` to prevent infinite middleware loops. Attackers could set this header externally to **skip all middleware**, bypassing every auth check. Affected Next.js 11.1.4 through 15.2.2 [37][38]. **Lesson: never trust internal-only headers from external requests.**

**CVE-2026-32130** — ZITADEL's SCIM API auth middleware failed to decode URL-encoded path characters while the routing layer did, creating a **desynchronization** that let unauthenticated attackers access user data [39]. **Lesson: path normalization must be consistent between auth and routing layers.**

**CVE-2026-39339** (CVSS 9.1) — ChurchCRM's middleware checked if the URL contained the string "api/public" to skip auth. Attackers prepended `/api/public/` to any path to bypass authentication entirely [40]. **Lesson: naive string matching on paths is catastrophically fragile.**

The most dangerous bypass vector is **path normalization desynchronization** — where the auth middleware and the routing layer normalize paths differently. Common attack variations include double slashes (`//admin`), path traversal (`/../admin`), URL encoding (`/%2e%2e/admin`), mixed case, and trailing slashes [41].

**Essential mitigations:**

- **Normalize paths before auth** — decode URL encoding, collapse multiple slashes, resolve `..` sequences, strip trailing slashes. This is the single most important protection.
- **Deny by default** — protect everything, then explicitly allow public paths. The code above defaults to `protected:user` for unmatched paths.
- **Strip override headers** — reject requests containing `X-Original-URL`, `X-Rewrite-URL`, or any internal-bypass headers from external sources [41][42].
- **Defense-in-depth** — even with middleware auth, use Starlette's `@requires()` decorator as a secondary check at the endpoint level [8].
- **Integration tests for auth** — test every endpoint with no auth, invalid auth, and wrong-level auth. Test path variations: `//path`, `/./path`, `/%2e%2e/path`, `/path/`, case variations [42].

---

## Part IV — Clean architecture and identity injection

### Every architecture tradition externalizes auth

Robert C. Martin's Clean Architecture places auth in the outermost ring — the "Frameworks and Drivers" layer. The dependency rule mandates that "source code dependencies can only point inwards" and that entities "would not expect to be affected by a change to page navigation, or security" [1]. Authentication is treated alongside the database and web framework as a **detail**.

Hexagonal Architecture (Ports and Adapters) treats auth as a **driver adapter** on the incoming side — it intercepts requests, validates credentials, and invokes the application's ports with the validated identity as a simple data structure. The hexagon contains core business logic only [43][44]. DDD classifies authentication as a **generic subdomain** — "problems that many companies share, often solved with off-the-shelf solutions" — and a separate bounded context from core domain logic [45].

Real-world systems that successfully added auth as a layer without modifying business logic include: API gateways (Kong, Envoy, AWS API Gateway) where backends contain zero auth code; Istio service mesh where Envoy sidecars handle auth transparently; and Starlette's own `AuthenticationMiddleware` which sets `request.user` without endpoint code needing auth imports [4][8][46].

### ASGI scope is the correct injection mechanism

Five patterns exist for making user identity available to apps. For a single-process platform with mounted sub-apps, **ASGI scope modification** is the clear winner [7][8]:

| Pattern | Cross-Mount? | Security | Complexity |
|---------|:---:|---|---|
| Request headers (`X-User-ID`) | ✅ | ⚠️ Header injection risk if not stripped | Low |
| **ASGI scope** (`scope["state"]["user_id"]`) | **✅** | **✅ Tamper-proof in-process** | **Low** |
| Python `contextvars` | ✅ | ✅ | Medium (propagation bugs) |
| FastAPI `Depends()` | ❌ | ✅ | Low per-app |
| Thread-local storage | ❌ | ❌ Data leakage in async | Legacy |

FastAPI's `Depends()` **does not cross mount boundaries** — dependencies are resolved per-app, not across mounts [47]. `contextvars` work but suffer propagation bugs when `BaseHTTPMiddleware` or `anyio.TaskGroup` is in the stack [11][48]. Headers introduce injection risk unless the middleware strips all client-provided identity headers first [6].

ASGI scope modification is how Starlette's own `AuthenticationMiddleware` works — it sets `scope["user"]` and `scope["auth"]` which are then accessible via `request.user` and `request.auth` in any downstream code [8]. The scope dict persists through the entire ASGI call chain including through Mount boundaries [7]. Sub-apps access identity via `request.state.user_id` with zero auth imports:

```python
# Sub-app endpoint — ZERO auth awareness
async def get_preferences(request: Request):
    user_id = request.state.user_id   # set by platform middleware
    store = request.state.user_store  # pre-scoped MutableMapping
    return store.get("preferences", {})
```

### Pre-scoped stores keep apps auth-unaware

For mapping authenticated identity to per-user data, three patterns exist. **Pre-scoped store injection** is the cleanest because apps receive a `MutableMapping` that "just works" — already scoped to the authenticated user by the platform, requiring zero auth awareness [49].

This mirrors how multi-tenant SaaS platforms scope data. ABP.io automatically appends `WHERE TenantId = ?` to all queries through the repository pattern so business logic is "multi-tenancy unaware" [50]. AWS S3 uses prefix-based tenant isolation (`s3://bucket/tenant-id/file`) [51]. The MutableMapping equivalent is key prefixing:

```python
from collections.abc import MutableMapping

class PrefixedStore(MutableMapping):
    """Scopes a MutableMapping to a key prefix. dol-compatible."""
    def __init__(self, base: MutableMapping, prefix: str):
        self._base = base
        self._prefix = prefix

    def __getitem__(self, key):
        return self._base[f"{self._prefix}{key}"]

    def __setitem__(self, key, value):
        self._base[f"{self._prefix}{key}"] = value

    def __delitem__(self, key):
        del self._base[f"{self._prefix}{key}"]

    def __iter__(self):
        n = len(self._prefix)
        for k in self._base:
            if k.startswith(self._prefix):
                yield k[n:]

    def __len__(self):
        return sum(1 for _ in self)

# Platform middleware injects pre-scoped store
class StoreInjectionMiddleware:
    def __init__(self, app, base_store: MutableMapping):
        self.app = app
        self.base_store = base_store

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("user"):
            user = scope["user"]
            if hasattr(user, "identity"):
                scope.setdefault("state", {})
                scope["state"]["user_store"] = PrefixedStore(
                    self.base_store, prefix=f"user/{user.identity}/"
                )
        await self.app(scope, receive, send)
```

Using `dol`'s own `wrap_kvs` achieves the same result more concisely [49]:

```python
from dol import wrap_kvs
user_store = wrap_kvs(base_store,
    key_of_id=lambda k: f"user/{user_id}/{k}",
    id_of_key=lambda k: k.removeprefix(f"user/{user_id}/"))
```

### Wiring it all together

The complete platform assembly mounts sub-apps, layers middleware in correct order, and provides platform-level login/logout routes:

```python
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.middleware import Middleware

# ── App registry (from app.toml / platform.toml) ──
AUTH_CONFIG = {
    "/api/health":     "public",
    "/api/blog":       "public",
    "/api/dashboard":  "protected:shared",
    "/api/notes":      "protected:user",
    "/auth/":          "public",        # login routes themselves are public
}

# ── Platform assembly ──
app = Starlette(
    routes=[
        # Platform-level auth routes
        Route("/auth/login", login_page, methods=["GET"]),
        Route("/auth/login", handle_login, methods=["POST"]),
        Route("/auth/shared-login", shared_login, methods=["POST"]),
        Route("/auth/logout", handle_logout, methods=["POST"]),
        Route("/auth/login/google", login_google),
        Route("/auth/callback", auth_callback),
        # Mounted sub-apps — zero auth awareness
        Mount("/api/health", app=health_app),
        Mount("/api/blog", app=blog_app),
        Mount("/api/dashboard", app=dashboard_app),
        Mount("/api/notes", app=notes_app),
    ],
    middleware=[
        # ⚠️ ORDER MATTERS: outermost middleware runs FIRST
        # Auth middleware is first — nothing bypasses it
        Middleware(PlatformAuthMiddleware,
            auth_config=AUTH_CONFIG,
            session_backend=session_store),
        # Store injection runs after auth (needs scope["user"])
        Middleware(StoreInjectionMiddleware, base_store=data_store),
    ],
)
```

---

## What to build now vs. what to plan for later

**Build now** (shared-password gates):

- Pure ASGI `PlatformAuthMiddleware` with path-dispatching against config registry
- Shared-password login form + `itsdangerous`-signed session cookies
- `hmac.compare_digest()` for password checks, Argon2id hashes in config
- Path normalization as the first middleware operation
- Deny-by-default for unmatched paths

**Build next** (per-user auth):

- Server-side session store using `MutableMapping` (start with `dict`, graduate to SQLite-backed `dol` store)
- Cookie-based sessions with `HttpOnly; Secure; SameSite=Lax`
- Platform login/logout routes
- `PrefixedStore` for user-scoped data injection via `request.state.user_store`
- Signed double-submit CSRF cookies via `starlette-csrf`
- WebSocket Origin validation

**Plan for later** (OAuth2/OIDC):

- Authlib integration for "Sign in with Google/GitHub" at the platform level
- External IdP evaluation: start with Authelia or Pocket ID, scale to Logto or ZITADEL
- Do not run a separate IdP until you need SSO across independent services or externally-managed MFA

---

## Conclusion

The research confirms that **ASGI middleware is the correct and well-supported mechanism** for implementing platform-level auth in a modular FastAPI application. Starlette's own `AuthenticationMiddleware` validates this pattern — it is pure ASGI, propagates to mounted sub-apps, handles both HTTP and WebSocket, and sets `scope["user"]` without downstream code needing auth imports. The critical implementation details are: always normalize paths before auth checks (the primary cause of real-world bypasses), default to deny for unmatched paths, use cookie-based sessions over JWTs for single-domain SPAs, and inject user identity via ASGI scope rather than headers or `contextvars`. The `PrefixedStore` pattern over `MutableMapping` elegantly solves per-user data scoping while keeping apps completely auth-unaware — mirroring how production multi-tenant platforms isolate data at the repository layer. Cookie sessions with `SameSite=Lax` plus signed double-submit CSRF tokens provide the right security posture without enterprise-grade complexity.

---

## REFERENCES

[1] R. C. Martin, ["The Clean Architecture,"](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html) The Clean Code Blog, Aug. 2012.

[2] Kong Inc., ["Key Authentication Plugin,"](https://developer.konghq.com/plugins/key-auth/) Kong Developer Portal.

[3] Traefik Labs, ["ForwardAuth Middleware,"](https://doc.traefik.io/traefik/v3.4/middlewares/http/forwardauth/) Traefik v3.4 Documentation.

[4] Envoy Project, ["External Authorization Filter,"](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/ext_authz_filter) Envoy Documentation.

[5] Amazon Web Services, ["Use API Gateway Lambda Authorizers,"](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-use-lambda-authorizer.html) AWS Documentation.

[6] M. Uzukwu, ["Why Early Request Header Modification Matters in API Gateways,"](https://dev.to/uzukwu_michael_91a95b823b/why-early-request-header-modification-matters-in-api-gateways-4gle) DEV Community.

[7] ASGI Team, ["ASGI Specification,"](https://asgi.readthedocs.io/en/latest/specs/main.html) ASGI Documentation.

[8] Encode, ["Authentication,"](https://www.starlette.io/authentication/) Starlette Documentation.

[9] Encode, ["Middleware,"](https://www.starlette.io/middleware/) Starlette Documentation.

[10] M. Kludex, ["Mount Middleware Support,"](https://github.com/encode/starlette/pull/1649) Starlette GitHub PR #1649.

[11] M. Kludex, ["BaseHTTPMiddleware Remaining Bugs,"](https://github.com/encode/starlette/discussions/1729) Starlette GitHub Discussion #1729.

[12] M. Kludex, ["BaseHTTPMiddleware Deprecation Discussion,"](https://github.com/encode/starlette/discussions/2160) Starlette GitHub Discussion #2160.

[13] OWASP, ["Authentication Cheat Sheet,"](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html) OWASP Cheat Sheet Series.

[14] OWASP, ["Session Management Cheat Sheet,"](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) OWASP Cheat Sheet Series.

[15] OWASP, ["Password Storage Cheat Sheet,"](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html) OWASP Cheat Sheet Series.

[16] Auth0, ["Token Storage,"](https://auth0.com/docs/secure/security-guidance/data-security/token-storage) Auth0 Security Guidance.

[17] OWASP, ["HTML5 Security Cheat Sheet,"](https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html) OWASP Cheat Sheet Series.

[18] I. London, ["Stop Using JWTs for Sessions,"](https://ianlondon.github.io/posts/dont-use-jwts-for-sessions/) ianlondon.github.io.

[19] OWASP, ["Cross-Site Request Forgery Prevention Cheat Sheet,"](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html) OWASP Cheat Sheet Series.

[20] Code Specialist, ["fastapi-auth-middleware,"](https://github.com/code-specialist/fastapi-auth-middleware) GitHub.

[21] A. Oleshkevich, ["imia — Auth Framework for Starlette,"](https://github.com/alex-oleshkevich/imia) GitHub.

[22] fastapi-users, ["fastapi-users,"](https://github.com/fastapi-users/fastapi-users) GitHub.

[23] Authlib, ["Starlette OAuth Client,"](https://docs.authlib.org/en/latest/client/fastapi.html) Authlib Documentation.

[24] Auth0, ["FastAPI Authentication Guide,"](https://developer.auth0.com/resources/guides/web-app/fastapi/basic-authentication) Auth0 Developer Resources.

[25] ZITADEL, ["FastAPI + Authlib Example,"](https://github.com/zitadel/example-auth-fastapi) GitHub.

[26] Pocket ID, ["Pocket ID,"](https://github.com/pocket-id/pocket-id) GitHub.

[27] Elest.io, ["Authentik vs Authelia vs Keycloak: Choosing the Right Self-Hosted IdP in 2026,"](https://blog.elest.io/authentik-vs-authelia-vs-keycloak-choosing-the-right-self-hosted-identity-provider-in-2026/) Elest.io Blog.

[28] Logto, ["Logto,"](https://github.com/logto-io/logto) GitHub.

[29] ZITADEL, ["ZITADEL,"](https://github.com/zitadel/zitadel) GitHub.

[30] PortSwigger, ["Bypassing SameSite Cookie Restrictions,"](https://portswigger.net/web-security/csrf/bypassing-samesite-restrictions) PortSwigger Web Security Academy.

[31] OWASP, ["SameSite Cookie Attribute,"](https://owasp.org/www-community/SameSite) OWASP Community.

[32] frankie567, ["starlette-csrf,"](https://github.com/frankie567/starlette-csrf) GitHub.

[33] OWASP, ["WebSocket Security Cheat Sheet,"](https://cheatsheetseries.owasp.org/cheatsheets/WebSocket_Security_Cheat_Sheet.html) OWASP Cheat Sheet Series.

[34] PortSwigger, ["Cross-Site WebSocket Hijacking,"](https://portswigger.net/web-security/websockets/cross-site-websocket-hijacking) PortSwigger Web Security Academy.

[35] C. Schneider, ["Cross-Site WebSocket Hijacking,"](https://www.christian-schneider.net/blog/cross-site-websocket-hijacking/) Blog.

[36] OWASP, ["Authorization Cheat Sheet,"](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html) OWASP Cheat Sheet Series.

[37] ProjectDiscovery, ["Next.js Middleware Authorization Bypass,"](https://projectdiscovery.io/blog/nextjs-middleware-authorization-bypass) ProjectDiscovery Blog.

[38] Datadog Security Labs, ["Next.js Middleware Auth Bypass,"](https://securitylabs.datadoghq.com/articles/nextjs-middleware-auth-bypass/) Datadog Blog.

[39] SentinelOne, ["CVE-2026-32130 — ZITADEL SCIM API Auth Bypass,"](https://www.sentinelone.com/vulnerability-database/cve-2026-32130/) SentinelOne Vulnerability Database.

[40] TheHackerWire, ["ChurchCRM Critical API Auth Bypass,"](https://www.thehackerwire.com/churchcrm-critical-api-auth-bypass/) TheHackerWire.

[41] OWASP, ["Testing for Bypassing Authorization Schema,"](https://owasp.org/www-project-web-security-testing-guide/v42/4-Web_Application_Security_Testing/05-Authorization_Testing/02-Testing_for_Bypassing_Authorization_Schema) OWASP Testing Guide.

[42] OWASP, ["A01:2025 — Broken Access Control,"](https://owasp.org/Top10/2025/A01_2025-Broken_Access_Control/) OWASP Top 10.

[43] A. Cockburn, ["Hexagonal Architecture,"](https://en.wikipedia.org/wiki/Hexagonal_architecture_(software)) Wikipedia.

[44] AWS, ["Hexagonal Architecture,"](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/hexagonal-architecture.html) AWS Prescriptive Guidance.

[45] CodeOpinion, ["Authorization — Domain or Application Layer?,"](https://codeopinion.com/authorization-domain-or-application-layer/) CodeOpinion Blog.

[46] Microsoft, ["API Gateway Pattern,"](https://learn.microsoft.com/en-us/dotnet/architecture/microservices/architect-microservice-container-applications/direct-client-to-microservice-communication-versus-the-api-gateway-pattern) Microsoft .NET Architecture Guide.

[47] S. Ramírez, ["FastAPI Advanced Middleware,"](https://fastapi.tiangolo.com/advanced/middleware/) FastAPI Documentation.

[48] Akarshan, ["FastAPI request.state vs Context Variables,"](https://dev.to/akarshan/fastapi-requeststate-vs-context-variables-when-to-use-what-2c07) DEV Community.

[49] i2mint, ["dol — Data Object Layer,"](https://github.com/i2mint/dol) GitHub.

[50] ABP Framework, ["Multi-Tenancy,"](https://docs.abp.io/en/abp/latest/Multi-Tenancy) ABP Documentation.

[51] AWS, ["SaaS Tenant Isolation,"](https://docs.aws.amazon.com/whitepapers/latest/saas-architecture-fundamentals/tenant-isolation.html) AWS Whitepapers.