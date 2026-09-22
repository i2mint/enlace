# enlace.frontend

SPA-aware static file serving for enlace.

Starlette’s StaticFiles returns 404 for paths that don’t map to a file on disk.
SPAs with client-side routing (React Router, Next.js, etc.) need unmatched paths
to fall back appropriately so the JS router can handle them.

For Next.js static exports with dynamic routes (e.g. `[id]`), the build
produces files like `projects/_.html` where `_` is the placeholder
from `generateStaticParams`.  This module resolves
`/projects/<any-uuid>` → `projects/_.html` so the correct page shell
is served and the client JS can read the real param from the URL.

Every static mount enlace makes also tells browsers to \*\*revalidate HTML
documents\*\* (`Cache-Control: no-cache`) — see [`RevalidatingStaticFiles`](#enlace.frontend.RevalidatingStaticFiles).

### Functions

| [`is_html_path`](#enlace.frontend.is_html_path)(path)   | Whether *path* names an HTML document, judged by its file extension.   |
|-----------------------------------------------------------------------|------------------------------------------------------------------------|

### Classes

| [`LandingWithUnknownApp404`](#enlace.frontend.LandingWithUnknownApp404)(\*, landing_dir)    | ASGI app that serves the landing frontend at `/` and 404s elsewhere.   |
|-----------------------------------------------------------------------------------------------|------------------------------------------------------------------------|
| [`RevalidatingStaticFiles`](#enlace.frontend.RevalidatingStaticFiles)(\*args[, ...])       | `StaticFiles` that makes browsers revalidate HTML documents.           |
| [`SPAStaticFiles`](#enlace.frontend.SPAStaticFiles)(\*args[, html_cache_control]) | StaticFiles subclass with SPA / Next.js dynamic-route fallback.        |

### *class* enlace.frontend.LandingWithUnknownApp404(, landing_dir)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

ASGI app that serves the landing frontend at `/` and 404s elsewhere.

Wraps `StaticFiles` for the landing app so that:

- `/`, `/index.html`, and any real file in the landing dir (e.g.
  `/assets/index-abc.js`) are served as usual.
- Any other path returns a friendly 404 HTML page with a link back to
  `/`, instead of silently falling back to the landing’s `index.html`.

Per-app SPA mounts (`/{name}/...`) live on more-specific Starlette
mounts that are registered earlier and take precedence over this one,
so SPA client-side routing for known apps still works. This wrapper
only handles paths that fall through to `/`.

### *class* enlace.frontend.RevalidatingStaticFiles(\*args, html_cache_control='no-cache', \*\*kwargs)

Bases: `StaticFiles`

`StaticFiles` that makes browsers revalidate HTML documents.

Starlette sends `ETag` and `Last-Modified` but no `Cache-Control`.
Without one, browsers apply *heuristic* freshness (RFC 9111 §4.2.2,
typically 10% of the time since `Last-Modified`) and reuse a page
without asking. For a built frontend that is the worst file to be stale:
the HTML names the (cache-busted) asset URLs, so a stale document keeps
loading the *previous* build in full, and a correct deploy looks failed.

HTML files (including SPA fallbacks to `index.html` and `304`
revalidations) get `Cache-Control: html_cache_control` — `no-cache`
by default: the browser may keep its copy but must revalidate, which the
existing `ETag` makes a cheap `304`. Non-HTML assets are untouched, and
a `Cache-Control` already on the response is never overridden. Pass
`html_cache_control=None` to opt out.

#### file_response(full_path, stat_result, scope, status_code=200)

Build the file response, adding `Cache-Control` for HTML files.

### *class* enlace.frontend.SPAStaticFiles(\*args, html_cache_control='no-cache', \*\*kwargs)

Bases: [`RevalidatingStaticFiles`](#enlace.frontend.RevalidatingStaticFiles)

StaticFiles subclass with SPA / Next.js dynamic-route fallback.

Resolution order for a request path:

1. Exact file match (normal StaticFiles behaviour).
2. Replace each unresolvable path segment with `_` (Next.js dynamic
   param placeholder) and try again — e.g.
   `projects/abc123` → `projects/_.html`.
3. Fall back to `/index.html` (classic SPA catch-all).

#### *async* get_response(path, scope)

Returns an HTTP response, given the incoming path, method and request headers.

* **Return type:**
  `Response`

### enlace.frontend.is_html_path(path)

Whether *path* names an HTML document, judged by its file extension.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)
