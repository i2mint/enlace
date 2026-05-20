"""SPA-aware static file serving for enlace.

Starlette's StaticFiles returns 404 for paths that don't map to a file on disk.
SPAs with client-side routing (React Router, Next.js, etc.) need unmatched paths
to fall back appropriately so the JS router can handle them.

For Next.js static exports with dynamic routes (e.g. ``[id]``), the build
produces files like ``projects/_.html`` where ``_`` is the placeholder
from ``generateStaticParams``.  This module resolves
``/projects/<any-uuid>`` → ``projects/_.html`` so the correct page shell
is served and the client JS can read the real param from the URL.
"""

from pathlib import Path

import anyio
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send


class SPAStaticFiles(StaticFiles):
    """StaticFiles subclass with SPA / Next.js dynamic-route fallback.

    Resolution order for a request path:

    1. Exact file match (normal StaticFiles behaviour).
    2. Replace each unresolvable path segment with ``_`` (Next.js dynamic
       param placeholder) and try again — e.g.
       ``projects/abc123`` → ``projects/_.html``.
    3. Fall back to ``/index.html`` (classic SPA catch-all).
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        # 1. Try the exact path first.
        response = await self._try_resolve(path, scope)
        if response is not None:
            return response

        # 2. Try replacing dynamic segments with "_".
        wildcard_path = await self._resolve_with_wildcards(path)
        if wildcard_path is not None:
            response = await self._try_resolve(wildcard_path, scope)
            if response is not None:
                return response

        # 3. Fallback to index.html (classic SPA catch-all).
        return await super().get_response("index.html", scope)

    async def _try_resolve(self, path: str, scope: Scope) -> Response | None:
        """Attempt normal StaticFiles resolution; return None on 404."""
        try:
            response = await super().get_response(path, scope)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        if response.status_code == 404:
            return None
        return response

    async def _resolve_with_wildcards(self, path: str) -> str | None:
        """Walk *path* segments, replacing any that don't exist on disk with ``_``.

        Returns the rewritten path if a valid match is found, else None.

        Next.js static export for a dynamic route ``[id]`` with
        ``generateStaticParams`` returning ``[{id: "_"}]`` produces::

            projects/_.html   ← the HTML page shell
            projects/_/       ← RSC data directory

        For a request like ``projects/abc123``, ``abc123`` doesn't exist on
        disk but ``_`` does (as both a directory and, with ``.html``, a file).
        We rewrite to ``projects/_`` and let ``StaticFiles(html=True)``
        resolve ``_.html`` — but only if ``_`` isn't **also** a directory
        (which would take priority and fail to find ``index.html`` inside).
        So we explicitly try ``_.html`` when the wildcard is a directory.
        """
        segments = [s for s in path.strip("/").split("/") if s]
        if not segments:
            return None

        resolved: list[str] = []
        changed = False

        for segment in segments:
            candidate = "/".join(resolved + [segment]) if resolved else segment
            _, stat_result = await anyio.to_thread.run_sync(self.lookup_path, candidate)
            if stat_result is not None:
                resolved.append(segment)
            else:
                # Try the wildcard placeholder instead.
                wildcard_candidate = "/".join(resolved + ["_"]) if resolved else "_"
                _, stat_w = await anyio.to_thread.run_sync(
                    self.lookup_path, wildcard_candidate
                )
                if stat_w is not None:
                    resolved.append("_")
                    changed = True
                else:
                    return None  # Neither the real segment nor "_" exists.

        if not changed:
            return None

        # The rewritten path ends with "_" which may be a directory.
        # StaticFiles(html=True) would look for _/index.html inside it,
        # but Next.js puts the page at _.html (sibling, not child).
        # Try the explicit .html path first.
        html_path = (
            "/".join(resolved[:-1] + [resolved[-1] + ".html"]) if resolved else None
        )
        if html_path:
            _, stat_html = await anyio.to_thread.run_sync(self.lookup_path, html_path)
            if stat_html is not None:
                return html_path

        return "/".join(resolved)


_NOT_FOUND_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>App not found</title>
<style>
 body{font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;
      background:#0f1115;color:#e6e8eb;margin:0;min-height:100vh;
      display:grid;place-items:center;padding:20px}
 .card{max-width:480px;background:#171a21;border:1px solid #2a2e38;
      border-radius:12px;padding:28px 32px;text-align:center}
 h1{margin:0 0 10px;font-size:22px;font-weight:600}
 p{margin:0 0 18px;color:#c4c8d0}
 a.btn{display:inline-block;background:#7cc4ff;color:#0a1420;
      text-decoration:none;font-weight:600;padding:9px 18px;border-radius:8px}
 a.btn:hover{background:#9aa6ff}
 code{background:#0f1115;padding:1px 6px;border-radius:4px;
      border:1px solid #2a2e38;color:#e6e8eb;word-break:break-all}
</style></head><body>
<div class="card">
<h1>App not found</h1>
<p>No app is registered at <code>{path}</code>.</p>
<a class="btn" href="/">See available apps</a>
</div></body></html>"""


class LandingWithUnknownApp404:
    """ASGI app that serves the landing frontend at ``/`` and 404s elsewhere.

    Wraps ``StaticFiles`` for the landing app so that:

    - ``/``, ``/index.html``, and any real file in the landing dir (e.g.
      ``/assets/index-abc.js``) are served as usual.
    - Any other path returns a friendly 404 HTML page with a link back to
      ``/``, instead of silently falling back to the landing's ``index.html``.

    Per-app SPA mounts (``/{name}/...``) live on more-specific Starlette
    mounts that are registered earlier and take precedence over this one,
    so SPA client-side routing for known apps still works. This wrapper
    only handles paths that fall through to ``/``.
    """

    def __init__(self, *, landing_dir):
        self._dir = Path(landing_dir).resolve()
        self._files = StaticFiles(directory=str(landing_dir), html=True)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._files(scope, receive, send)
            return

        # Starlette mounts strip the mount prefix from path before delegating;
        # for the "/" mount, the path that arrives here is the original path
        # without leading "/". e.g. request "/foo" -> path "foo".
        rel = scope.get("path", "").lstrip("/")
        if not rel or rel == "index.html" or self._is_real_file(rel):
            await self._files(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        original = "/" + rel  # what the user actually typed
        if method not in ("GET", "HEAD"):
            await _send_plain_404(send, method)
            return
        body = _NOT_FOUND_PAGE.replace("{path}", _escape(original))
        data = body.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(data)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"" if method == "HEAD" else data})

    def _is_real_file(self, rel: str) -> bool:
        if ".." in rel.split("/"):
            return False
        try:
            candidate = (self._dir / rel).resolve()
        except OSError:
            return False
        try:
            candidate.relative_to(self._dir)
        except ValueError:
            return False
        return candidate.is_file()


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


async def _send_plain_404(send, method: str) -> None:
    body = b"" if method == "HEAD" else b"Not Found"
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
