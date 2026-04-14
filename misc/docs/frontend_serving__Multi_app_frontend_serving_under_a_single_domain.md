# Multi-app frontend serving under a single domain

**The simplest architecture for hosting 10–30 independent web apps under one domain is path-based routing with per-app SPA fallback, combined with server-side HTML injection for shared concerns.** This approach avoids the complexity of micro-frontend frameworks entirely while delivering independent development, independent deployment, and platform-level injection of analytics, auth, and navigation. The key insight: what looks like a micro-frontend problem is actually a static-file-serving problem with a thin injection layer. Caddy or Nginx handles routing and SPA fallback per path prefix, while a Python ASGI middleware (or Nginx's `sub_filter`) injects shared scripts into every app's HTML before it reaches the browser. Each app remains a standalone SPA or static site, blissfully unaware of the platform around it.

---

## Path-based routing makes multi-SPA serving straightforward

The core challenge is deceptively simple: serve multiple independent SPAs under path prefixes (`/apps/chord_analyzer/`, `/apps/todo/`) while ensuring that client-side routes within each app (e.g., `/apps/chord_analyzer/settings`) don't return 404. The server must serve actual static files when they exist and fall back to the app's `index.html` for everything else within that prefix.

**Caddy** handles this cleanly with `handle_path` and `try_files`:

```caddyfile
thorwhalen.com {
    encode zstd gzip

    handle /api/* {
        reverse_proxy localhost:8000
    }

    handle_path /apps/chord_analyzer/* {
        root * /srv/apps/chord_analyzer
        try_files {path} /index.html
        file_server
    }

    handle_path /apps/todo/* {
        root * /srv/apps/todo
        try_files {path} /index.html
        file_server
    }

    handle {
        root * /srv/launcher
        try_files {path} /index.html
        file_server
    }
}
```

The critical detail is `handle_path` versus `handle`. The `handle_path` directive **strips the matched prefix** before processing, so `/apps/chord_analyzer/settings` becomes `/settings` when looking up files on disk. The `try_files` directive checks if a real file exists at that path; if not, it rewrites to `/index.html` — the SPA fallback.

**Nginx** achieves the same with `alias` and `try_files`, but with more footguns:

```nginx
location /apps/chord_analyzer/ {
    alias /srv/apps/chord_analyzer/;
    try_files $uri $uri/ /apps/chord_analyzer/index.html;

    location ~* \.[a-f0-9]{8,}\.(js|css|woff2?|png|svg)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}
```

Three common Nginx pitfalls deserve emphasis. First, **use `alias`, not `root`** — `root` prepends the location path to the directory, doubling the prefix. Second, both the `location` and `alias` directives must either both have trailing slashes or both lack them. Third, the fallback path in `try_files` must be the **full URI** (`/apps/chord_analyzer/index.html`), not a relative path.

For **dynamic app discovery** without editing config per app, Nginx supports regex locations:

```nginx
location ~ ^/apps/([^/]+)/ {
    alias /srv/apps/$1/;
    try_files $uri $uri/ /apps/$1/index.html;
}
```

This matches any app name automatically but carries a caveat: Nginx regex locations with `alias` and variable capture are fragile across versions and should be tested carefully.

**Traefik** occupies a different niche — it's a reverse proxy and load balancer that doesn't serve static files. Each app would need its own upstream static-file server (an Nginx or Caddy container), with Traefik routing requests between them. This adds a layer of indirection that's unnecessary for a solo developer serving static files from disk.

---

## Python serves static files well enough for development

For a Python-based development server, Starlette's `StaticFiles` with `html=True` serves `index.html` for directory requests but **does not support SPA fallback** — requesting `/apps/todo/about` returns 404 because no file named `about` exists. The widely-used fix is a `SPAStaticFiles` subclass:

```python
from starlette.staticfiles import StaticFiles

class SPAStaticFiles(StaticFiles):
    """Serves index.html for any path that doesn't match a real file."""
    async def lookup_path(self, path: str):
        full_path, stat_result = await super().lookup_path(path)
        if stat_result is None:
            return await super().lookup_path("index.html")
        return full_path, stat_result
```

This enables auto-discovery and mounting of all apps:

```python
from pathlib import Path
from fastapi import FastAPI

app = FastAPI()
APPS_DIR = Path("./frontend_apps")

for app_dir in sorted(APPS_DIR.iterdir()):
    if app_dir.is_dir() and (app_dir / "index.html").exists():
        app.mount(
            f"/apps/{app_dir.name}",
            SPAStaticFiles(directory=str(app_dir), html=True),
            name=f"app_{app_dir.name}",
        )
```

Performance matters at scale but not at this scale. Nginx delivers roughly **4–5× the throughput** of Uvicorn/Starlette for static files (~900 MB/s vs ~200 MB/s with 4 workers). For a personal platform with modest traffic, this difference is irrelevant. The recommended production architecture is **Caddy serving static files directly** and reverse-proxying `/api/*` to the Python backend — Python never touches static file serving in production.

---

## Base path configuration requires coordination across three layers

When a React app built for `/` is deployed under `/apps/chord_analyzer/`, three things break simultaneously: asset URLs point to the wrong location, React Router navigates to wrong paths, and relative API calls resolve incorrectly. Fixing this requires aligning the build tool, the router, and the server.

**Vite's `base` config** rewrites all asset URLs at build time:

```js
// vite.config.js
export default defineConfig({
  base: process.env.VITE_BASE_PATH || '/apps/chord_analyzer/',
  plugins: [react()],
})
```

This produces `<script src="/apps/chord_analyzer/assets/main-a1b2c3.js">` in the built HTML and sets `import.meta.env.BASE_URL` to the configured value. The platform can inject the base path via environment variable before building: `VITE_BASE_PATH=/apps/chord_analyzer/ npm run build`.

**React Router's `basename`** must also be set, but with an inconsistency that trips everyone up: Vite's `base` requires a trailing slash (`/apps/chord_analyzer/`), while React Router's `basename` must **not** have one (`/apps/chord_analyzer`):

```jsx
const basename = import.meta.env.BASE_URL.replace(/\/$/, '')

const router = createBrowserRouter([
  { path: '/', element: <App />, children: [
    { index: true, element: <Home /> },
    { path: 'settings', element: <Settings /> },
  ]},
], { basename })
```

For **pre-built artifacts** where the base path wasn't set at build time, a hybrid approach works: build with `base: './'` (relative assets) and inject the router basename at serve time via a platform script:

```html
<script>window.__BASE_PATH__ = '/apps/chord_analyzer';</script>
```

The app reads `window.__BASE_PATH__` for its router basename, while relative asset URLs (`./assets/main.js`) load correctly from any directory. This approach has known edge cases with code-splitting in some Vite versions, so build-time injection via `VITE_BASE_PATH` remains the cleanest path.

| Framework | Config | Affects assets | Affects routing | Timing |
|-----------|--------|:-:|:-:|--------|
| Vite | `base: '/prefix/'` | ✅ | ❌ | Build |
| CRA | `homepage: '/prefix'` | ✅ | ❌ | Build |
| Next.js | `basePath: '/prefix'` | ✅ | ✅ | Build |
| React Router | `basename: '/prefix'` | ❌ | ✅ | Runtime |

---

## Most micro-frontend patterns are overkill here

The micro-frontend landscape offers six main approaches, but only two matter for a solo developer with independent apps.

**Simple path-based routing** — each app is a fully independent SPA under its own prefix, with shared concerns injected at serve time — is the **lowest-complexity, highest-value approach**. Zero framework overhead, any tech stack per app, trivial deployment (copy static files), trivial debugging (each app is self-contained). The only cost is a full page reload when navigating between apps.

**Server-side composition** (injecting shared HTML fragments at serve time) pairs perfectly with path-based routing. Nginx SSI, Cloudflare's HTMLRewriter, or Python ASGI middleware can inject nav headers, analytics, and auth state transparently into any app's HTML.

Everything else adds complexity without proportional benefit at this scale:

- **Webpack Module Federation** solves runtime shared-dependency negotiation between apps built by different teams. A solo developer can just use the same React version everywhere, loaded from a CDN URL. The governance overhead — shared dependency policies, `remoteEntry.js` caching, version skew debugging — exists to solve multi-team coordination problems.
- **single-spa and qiankun** manage application lifecycles (mount/unmount) for running multiple frameworks simultaneously in one page. This solves cross-team migration problems, not independent-app hosting.
- **Iframes** provide the strongest isolation but create UX seams: no shared scrolling, awkward deep linking, `postMessage`-based communication, and mobile responsiveness headaches.
- **Import maps** deserve a mention as the lightest-weight runtime composition option. Browser-native import maps (now at **~95% support**) can share dependencies across apps without bundler lock-in. For this platform, they're useful if apps need to share a runtime dependency (like React) without each bundling their own copy — but this is optimization, not architecture.

The industry is converging on this view. Reported micro-frontend adoption dropped from 75% to 24% as teams recognized most implementations added unnecessary complexity. For **10–30 apps developed by one person**, path-based routing with SSI is the architecture.

---

## Injecting shared concerns via ASGI middleware

The platform needs to inject analytics, auth state, and navigation into every app's HTML without apps knowing about these concerns. An ASGI middleware that intercepts HTML responses and performs string replacement is the most flexible approach for a Python-based platform:

```python
class HTMLInjectionMiddleware:
    def __init__(self, app, head_html="", body_prefix="", body_suffix=""):
        self.app = app
        self.head_injection = head_html.encode()
        self.body_prefix = body_prefix.encode()
        self.body_suffix = body_suffix.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_html = False
        body_chunks = []
        initial_message = None

        async def send_wrapper(message):
            nonlocal is_html, initial_message
            if message["type"] == "http.response.start":
                initial_message = message
                headers = dict(message.get("headers", []))
                content_type = headers.get(b"content-type", b"")
                is_html = b"text/html" in content_type
                if not is_html:
                    await send(message)
            elif message["type"] == "http.response.body":
                if not is_html:
                    await send(message)
                    return
                body_chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    full_body = b"".join(body_chunks)
                    full_body = full_body.replace(
                        b"</head>", self.head_injection + b"\n</head>", 1
                    )
                    import re
                    full_body = re.sub(
                        rb"(<body[^>]*>)", rb"\1\n" + self.body_prefix, full_body, count=1
                    )
                    full_body = full_body.replace(
                        b"</body>", self.body_suffix + b"\n</body>", 1
                    )
                    # Update Content-Length
                    new_headers = [
                        (k, v) for k, v in initial_message["headers"]
                        if k.lower() != b"content-length"
                    ]
                    new_headers.append(
                        (b"content-length", str(len(full_body)).encode())
                    )
                    initial_message["headers"] = new_headers
                    await send(initial_message)
                    await send({"type": "http.response.body", "body": full_body})

        await self.app(scope, receive, send_wrapper)
```

This middleware buffers HTML responses, injects content before `</head>` and after `<body>`, then updates the Content-Length header. Usage is composable:

```python
app = HTMLInjectionMiddleware(
    inner_app,
    head_html='''
        <script>window.__PLATFORM_USER__ = null;</script>
        <script src="/platform/analytics.js" defer></script>
        <script src="/platform/auth.js" defer></script>
    ''',
    body_prefix='<platform-nav></platform-nav>',
    body_suffix='<script src="/platform/nav-component.js"></script>',
)
```

For production behind **Nginx**, the built-in `sub_filter` module achieves the same result without Python involvement:

```nginx
location /apps/chord_analyzer/ {
    alias /srv/apps/chord_analyzer/;
    try_files $uri $uri/ /apps/chord_analyzer/index.html;

    sub_filter '</head>' '<script src="/shared/platform.js"></script></head>';
    sub_filter_once on;
    sub_filter_types text/html;
    proxy_set_header Accept-Encoding "";  # Required: sub_filter can't process gzip
}
```

**Caddy lacks a built-in equivalent** to `sub_filter`. The recommended workaround is build-time injection — adding `<script src="/shared/platform.js"></script>` to each app's `index.html` template, or using the third-party `replace-response` plugin (requires building Caddy from source with `xcaddy`).

For **analytics specifically**, Cloudflare's approach is instructive: for domains proxied through Cloudflare, it auto-injects the analytics beacon at the edge by modifying HTML before it reaches the client. This only fails if `Cache-Control: no-transform` is set. A platform middleware replicates this pattern at the application layer.

---

## Authentication works best as a platform gateway

For auth as a platform concern, the **forward-auth gateway pattern** is the production standard. A reverse proxy sends a subrequest to an auth service before forwarding to the app; the auth service returns 200 (allow) or 302 (redirect to login), along with user info headers.

**Authelia** is the most appropriate tool for a solo developer: an open-source SSO portal that works as a forward-auth companion to Caddy, Nginx, or Traefik. Session cookies scoped to the parent domain (`.thorwhalen.com`) enable single sign-on across all apps. Policy-based access control supports per-app rules (public, one-factor, two-factor).

For simpler setups, a **platform-injected auth script** avoids external dependencies entirely:

```javascript
// /platform/auth.js — injected into every app's <head>
(function() {
  fetch('/api/session', { credentials: 'same-origin' })
    .then(r => r.ok ? r.json() : null)
    .then(user => {
      window.__PLATFORM_USER__ = user;
      window.dispatchEvent(new CustomEvent('platform:auth', { detail: { user } }));
      if (user) {
        renderUserMenu(user);
      } else {
        renderLoginButton();
      }
    });
})();
```

Apps that need user context listen for the `platform:auth` event or read `window.__PLATFORM_USER__` — a clean contract that requires zero auth code inside any app.

---

## A Web Component provides isolated shared navigation

For the shared nav bar, a **Shadow DOM Web Component** provides the strongest CSS isolation — app styles can't break the nav, and nav styles can't break apps:

```javascript
// /platform/nav-component.js
class PlatformNav extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
  }
  connectedCallback() {
    const user = window.__PLATFORM_USER__;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; position:fixed; top:0; left:0; right:0;
                height:48px; z-index:999999;
                font-family:-apple-system,BlinkMacSystemFont,sans-serif; }
        nav { display:flex; align-items:center; height:100%;
              background:var(--platform-nav-bg,#1a1a2e); padding:0 16px;
              box-shadow:0 2px 4px rgba(0,0,0,0.2); }
        a { color:#a8d8ea; text-decoration:none; padding:8px 12px; }
        a:hover { background:rgba(255,255,255,0.1); border-radius:4px; }
        .brand { font-weight:700; font-size:16px; margin-right:24px; color:white; }
        .spacer { flex:1; }
      </style>
      <nav>
        <a href="/" class="brand">⚡ thorwhalen</a>
        <a href="/apps/">Apps</a>
        <span class="spacer"></span>
        ${user
          ? `<span>${user.name}</span> <a href="/platform/logout">Logout</a>`
          : `<a href="/login">Login</a>`}
      </nav>`;
    document.body.style.marginTop = '56px';
  }
}
customElements.define('platform-nav', PlatformNav);
```

CSS custom properties (`--platform-nav-bg`) cross the shadow boundary, enabling theming without breaking encapsulation. The alternative — injecting a plain `<div>` nav with aggressive CSS resets (`all: initial` on the container, `!important` on `body { margin-top }`) — works but is more fragile.

---

## Build orchestration should stay minimal

Each app owns its build process. The platform should **serve pre-built output, not run builds**. A shell script is the simplest orchestrator:

```bash
#!/bin/bash
# build_all.sh
set -e
for app_dir in apps/*/frontend; do
    [ -f "$app_dir/package.json" ] || continue
    app_name=$(basename "$(dirname "$app_dir")")
    echo "=== Building $app_name ==="
    (cd "$app_dir" && npm ci && VITE_BASE_PATH=/apps/$app_name/ npm run build)
done
```

A **Makefile** adds parallelism for free: `make -j4 build-all` builds four apps simultaneously. Turborepo and Nx are worth adopting only if apps start sharing code (a shared UI library, shared types); for fully independent apps, they add setup overhead without payoff.

The recommended directory structure:

```
platform/
├── apps/
│   ├── chord_analyzer/
│   │   └── frontend/
│   │       ├── dist/          # Served by Caddy/Nginx
│   │       ├── src/
│   │       ├── package.json
│   │       └── vite.config.js
│   └── static_tool/
│       └── frontend/
│           └── index.html     # No build step needed
├── shared/                    # Platform JS/CSS assets
├── build_all.sh
└── backend/
```

For development, Vite's built-in proxy handles API forwarding:

```js
// vite.config.js
export default defineConfig({
  base: '/apps/chord_analyzer/',
  server: {
    port: 5173,
    proxy: { '/api': 'http://localhost:8000' },
  },
})
```

A dev startup script can optionally run Caddy as a single entry point, routing the active app to Vite's dev server (with HMR) while serving other apps from their `dist/` directories.

---

## Caching is already solved by build tools

Vite and webpack produce content-hashed filenames by default (`main-a1b2c3.js`). The platform's only responsibility is setting correct `Cache-Control` headers:

| File type | Header | Why |
|-----------|--------|-----|
| Hashed assets (`*.a1b2c3.js`) | `public, max-age=31536000, immutable` | Hash changes when content changes; safe to cache forever |
| `index.html` | `no-cache` | Must revalidate so browsers fetch latest asset references |
| API responses | `no-store` | Dynamic data, never cache |

In Caddy, this is a per-app pattern:

```caddyfile
@hashed path_regexp \\.[0-9a-f]{8,}\\.(js|css|woff2?|png|svg)$
header @hashed Cache-Control "public, max-age=31536000, immutable"

@html path *.html
header @html Cache-Control "public, max-age=0, must-revalidate"
```

Service workers should be handled **per-app**, not at the platform level. A service worker at `/apps/chord_analyzer/sw.js` automatically scopes to that prefix, providing natural isolation.

---

## Prior art confirms the path-based approach

Existing platforms validate this architecture. **Vercel** supports multi-app domains via `rewrites` in `vercel.json`, routing path prefixes to separate deployments — essentially the same pattern as Caddy's `handle_path` blocks. **Backstage** (Spotify) takes a different approach: it's a monolithic React SPA where plugins are npm packages bundled together at build time — appropriate for developer portals but not independent apps. **Cloudflare Pages** can't natively serve multiple apps under path prefixes on one domain without a Workers-based routing layer.

Among micro-frontend frameworks, **Piral** comes closest to the indie platform concept (an app shell loading independent "pilets"), but it requires all apps to use the Piral API and is React-specific. **single-spa** and **qiankun** solve multi-team, multi-framework composition problems that don't exist for a solo developer.

---

## Conclusion: the recommended stack

The architecture that delivers the most value with the least complexity for a personal multi-app platform:

**Caddy** serves static files for all apps with per-prefix SPA fallback, handles automatic HTTPS, and reverse-proxies `/api/*` to the Python backend. **ASGI middleware** (or Nginx's `sub_filter` if using Nginx) injects a platform script into every HTML response — this single script loads analytics, checks auth state, and renders the shared nav bar via a Shadow DOM Web Component. Each app builds independently with `VITE_BASE_PATH` set by the platform, producing self-contained static assets with content-hashed filenames. A shell script or Makefile orchestrates builds; Docker Compose runs the production stack.

The key insight worth repeating: **this is not a micro-frontend problem**. It's a static-file-serving problem with a thin shared-concerns injection layer. The complexity budget should go toward building great apps, not toward architectural scaffolding. Path-based routing with server-side injection handles 10–30 independent apps with zero framework overhead, independent deployment, and a Caddyfile that grows by eight lines per app.