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

import anyio
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


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
            _, stat_result = await anyio.to_thread.run_sync(
                self.lookup_path, candidate
            )
            if stat_result is not None:
                resolved.append(segment)
            else:
                # Try the wildcard placeholder instead.
                wildcard_candidate = (
                    "/".join(resolved + ["_"]) if resolved else "_"
                )
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
        html_path = "/".join(resolved[:-1] + [resolved[-1] + ".html"]) if resolved else None
        if html_path:
            _, stat_html = await anyio.to_thread.run_sync(
                self.lookup_path, html_path
            )
            if stat_html is not None:
                return html_path

        return "/".join(resolved)
