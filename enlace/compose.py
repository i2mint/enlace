"""ASGI app composition for enlace.

Builds a single FastAPI application by mounting discovered sub-apps and applying
cross-cutting middleware. Handles lifespan cascading to mounted sub-apps
(Starlette does not do this natively).

Plugins:
    ``build_backend`` accepts a ``plugins`` argument — a sequence of callables
    ``(parent: FastAPI, config: PlatformConfig) -> None`` invoked once after
    sub-apps are mounted. ``enlace_auth.plugin`` is the canonical example:
    when installed, it adds auth, sessions, the admin dashboard, and per-user
    stores. enlace itself is auth-agnostic.

    ``build_backend`` publishes ``parent.state.app_manifest_readers`` (one live
    manifest reader per app) for plugins that build launcher items themselves;
    pass it to ``refresh_updated_at`` first.
"""

import contextlib
import importlib
import inspect
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Optional, Sequence

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route

from enlace.base import AppConfig, PlatformConfig
from enlace.discover import discover_apps, forget_partial_imports
from enlace.frontend import (
    LandingWithUnknownApp404,
    RevalidatingStaticFiles,
    SPAStaticFiles,
)
from enlace.gzip_selective import SelectiveGZipMiddleware
from enlace.manifest import (
    PLATFORM_MANIFEST_NAME,
    RUNTIME_VERSION_KEY,
    DeployHeadersMiddleware,
    DeployManifest,
    MANIFEST_ERROR_KEY,
    DeployMetaTagMiddleware,
    load_manifest,
    load_platform_manifest,
    resolve_manifest_dir,
)
from enlace.util import url_path

_logger = logging.getLogger("enlace")

Plugin = Callable[[FastAPI, PlatformConfig], None]


class EnlaceConfigError(RuntimeError):
    """Raised when platform configuration is unusable at startup.

    Distinct from ``ValueError`` / ``RuntimeError`` so callers and tests can
    target this specific class.
    """


def build_backend(
    config: PlatformConfig,
    *,
    plugins: Sequence[Plugin] = (),
    analytics_store: Optional[MutableMapping] = None,
) -> FastAPI:
    """Compose all app backends into a single ASGI application.

    For each discovered app:
    - mode=asgi, asgi_app: mount the ASGI object at the route prefix
    - mode=asgi, functions: build an APIRouter with POST routes and include it
    - mode=process/external: mount a reverse proxy at the route prefix
    - mode=static: mount StaticFiles at the route prefix
    - frontend_only (mode=asgi): skip (no backend to mount)

    An asgi app that cannot be imported or loaded does not take the platform
    down: a 500 stub is mounted in its place and the failure is reported in
    the logs, ``/_meta``, ``/_apps`` and ``enlace doctor`` (loud and local —
    see :mod:`enlace.degraded`). ``config.app_load_failure = "raise"``, or
    ``fail_hard = true`` in an app's app.toml, refuses to boot instead.
    Platform configuration errors always refuse to boot.

    Args:
        config: Platform configuration with apps already discovered.
        plugins: Compose-time plugins, e.g. ``enlace_auth.plugin``.
        analytics_store: Where page-view analytics go, for apps that opted in
            (any ``MutableMapping[str, dict]``, e.g. a ``dol`` store). Default:
            JSON files under ``[analytics].store_path``. See enlace.analytics.

    Returns:
        A FastAPI application with all sub-apps mounted.
    """
    # Signal to sub-apps that they're running under enlace, so they can skip
    # their own CORS middleware, standalone startup blocks, etc.
    os.environ["ENLACE_MANAGED"] = "1"

    sub_apps: list[tuple[str, str, object]] = []  # (route_prefix, app_id, asgi_app)

    # Dispatch every app through its registered BackendStrategy. Built-in
    # strategies cover asgi/process/external/static; plugins (e.g.
    # enlace_docker) contribute additional modes via entry points. Each
    # strategy's ``make_asgi`` returns either an ASGI sub-app to mount or
    # ``None`` (e.g. frontend-only apps, or apps whose port hasn't been
    # allocated yet).
    #
    # An asgi app that fails to load gets a 500 stub instead of taking every
    # other app down with it (see enlace.degraded). Only the per-app load is
    # contained; get_strategy's unknown-mode error and every other mode's
    # make_asgi error are configuration, and propagate.
    from enlace.degraded import load_or_stub
    from enlace.strategies import get_strategy

    for app_config in config.apps:
        strategy = get_strategy(app_config.mode)
        sub_app = load_or_stub(strategy, app_config, config)
        if sub_app is not None:
            sub_apps.append((app_config.route_prefix, app_config.name, sub_app))

    apps_by_name = {a.name: a for a in config.apps}

    @asynccontextmanager
    async def cascade_lifespan(app: FastAPI):
        """Forward startup/shutdown to mounted sub-apps.

        Starlette does NOT propagate lifespan events to mounted sub-apps
        (issue #649, open since Sept 2019). This workaround iterates Mount
        routes and enters their lifespan contexts.

        App mounts are ``_AppIdInjector`` wrappers, so the app whose lifespan
        runs is the wrapped one (until #76 the wrapper hid it, and no app's
        lifespan ever ran). An asgi app whose startup raises is swapped for a
        500 stub and reported, like an import failure (enlace.degraded); the
        other apps start. Its shutdown is not run: it never started.

        What the apps' lifespans yield (Starlette's lifespan state) is merged
        and yielded here, so ``request.state`` carries it as it would for a
        standalone app. The state is shared by every app on the gateway.
        """
        from enlace.degraded import enter_lifespan_or_stub

        state: dict = {}
        async with contextlib.AsyncExitStack() as stack:
            for route in app.routes:
                if not isinstance(route, Mount):
                    continue
                if isinstance(route.app, _AppIdInjector):
                    app_config = apps_by_name.get(route.app.app_id)
                    if app_config is not None:
                        app_state = await enter_lifespan_or_stub(
                            stack, route.app, app_config, config
                        )
                        state.update(app_state or {})
                        continue
                if hasattr(route.app, "router"):
                    lifespan = getattr(route.app.router, "lifespan_context", None)
                    if lifespan is not None:
                        await stack.enter_async_context(lifespan(route.app))
            yield state or None

    parent = FastAPI(
        title="enlace platform",
        lifespan=cascade_lifespan,
    )

    # CORS on the parent only — sub-apps must NOT add their own CORS
    _add_cors(parent, config.cors_origins)

    # Compose-time plugins (pure ASGI middleware only — no BaseHTTPMiddleware
    # in plugins either, please). enlace_auth is the canonical plugin: it
    # mounts /auth/*, /_admin/*, store routes, and the auth+csrf middleware.
    for plug in plugins:
        plug(parent, config)

    # Deploy manifests, read once here. Built before /_apps, whose "last
    # updated" reads them too (#84).
    manifest_dir = resolve_manifest_dir(config.manifest_dir)
    enlace_version = _detect_enlace_version()
    platform_manifest = load_platform_manifest(
        manifest_dir, enlace_version=enlace_version
    )
    per_app_manifests: dict[str, DeployManifest] = {
        app.name: load_manifest(app.name, manifest_dir, enlace_version=enlace_version)
        for app in config.apps
    }
    _stamp_updated_at(config, per_app_manifests)
    # One reader per manifest file, shared by /_apps, the /_meta routes and the
    # deploy-identity middlewares below, so a manifest rewritten while the
    # backend runs shows up everywhere on the next request (#49, #81).
    platform_meta = _MtimeCachedManifest(
        platform_manifest, manifest_dir=manifest_dir, enlace_version=enlace_version
    )
    app_metas = {
        name: _MtimeCachedManifest(
            manifest,
            name=name,
            manifest_dir=manifest_dir,
            enlace_version=enlace_version,
        )
        for name, manifest in per_app_manifests.items()
    }

    # Published for plugins that build launcher items themselves (enlace_auth's
    # metadata edit), so they re-stamp ``updated_at`` from the live manifest
    # rather than from whatever the last GET /_apps left (#87). See
    # ``refresh_updated_at``.
    parent.state.app_manifest_readers = app_metas

    # JSON listing is always on (cheap, useful for frontends even when the
    # HTML index_page is disabled).
    _add_apps_listing_route(parent, config, app_metas)

    # Privacy-first analytics, only when some app opted in. Its routes (the
    # opt-out page) go in now, before any catch-all "/" mount can shadow them;
    # its middleware goes in with the others below.
    from enlace.analytics import make_analytics

    analytics = make_analytics(config, store=analytics_store)
    if analytics is not None:
        analytics.add_routes(parent)

    # Deploy manifest endpoints + response headers. Built once at startup;
    # cheap (one HTTP route per app + a small middleware). Endpoints are
    # registered BEFORE sub-app mounts so they win the path match.
    _add_meta_routes(
        parent, config, platform_meta, app_metas, enlace_version=enlace_version
    )

    # landing_app takes precedence over the default Python index. When set
    # to a discovered app's name, the later frontend-mount loop will mount
    # that app's frontend at / (see below). The built-in index is skipped.
    landing_app_name = config.landing_app
    if landing_app_name and not any(a.name == landing_app_name for a in config.apps):
        raise ValueError(
            f"platform.landing_app = {landing_app_name!r} but no such app "
            f"was discovered. Known apps: "
            f"{sorted(a.name for a in config.apps)}"
        )

    if config.index_page and not landing_app_name:
        _add_index_route(parent, config)

    for prefix, app_id, sub_app in sub_apps:
        _add_trailing_slash_redirect(parent, prefix)
        parent.mount(prefix, _AppIdInjector(sub_app, app_id))

    # Serve frontend static files for apps that have a frontend/ directory.
    # Mounted at /{app_name}/ so the frontend is accessible alongside the API.
    # Uses SPAStaticFiles so client-side routing (e.g. /projects/{id}) falls
    # back to index.html instead of returning 404.
    # The landing_app (if any) is mounted LAST at / so it catches the root.
    landing_app_config: Optional[AppConfig] = None
    for app_config in config.apps:
        if (
            app_config.mode != "static"
            and app_config.frontend_dir
            and app_config.frontend_dir.is_dir()
        ):
            if app_config.name == landing_app_name:
                landing_app_config = app_config
                # Still also mount at /{name}/ so the app is reachable by
                # name; some deployments may link to both.
            frontend_prefix = f"/{app_config.name}"

            _add_trailing_slash_redirect(parent, frontend_prefix)
            parent.mount(
                frontend_prefix,
                SPAStaticFiles(directory=str(app_config.frontend_dir), html=True),
            )

    # Trailing-slash redirects for router roots contributed by plugins (e.g.
    # enlace_auth's /_admin/). App and frontend prefixes already got theirs at
    # mount time; this covers prefix-mounted routers, which would otherwise
    # 404 at the bare /prefix once the catch-all "/" mount shadows Starlette's
    # built-in redirect. Run after all routers are registered.
    _add_router_root_redirects(parent)

    # landing_app: mount the chosen app's frontend at / as well, so the
    # platform's root URL serves it instead of the default Python index.
    # Unknown top-level paths (e.g. typo'd app names) get a 404 page with a
    # link back to / instead of falling back to the landing index.html, which
    # would silently disguise broken links.
    if landing_app_config is not None:
        parent.mount(
            "/",
            LandingWithUnknownApp404(
                landing_dir=landing_app_config.frontend_dir,
            ),
        )

    # Serve platform-level shared assets (e.g. shared.css) at the root.
    # Mounted last so it never shadows API or app-specific routes.
    if config.shared_assets_dir and config.shared_assets_dir.is_dir():
        parent.mount(
            "/",
            RevalidatingStaticFiles(directory=str(config.shared_assets_dir)),
        )

    # Deploy-identity response headers. Build the prefix → manifest map from
    # both the API mount (route_prefix) and the frontend mount (/{name}/) so
    # static asset responses also carry the headers.
    headers_prefix_map: dict[str, Callable[[], DeployManifest]] = {}
    for app_config in config.apps:
        app_meta = app_metas.get(app_config.name)
        if app_meta is None:
            continue
        headers_prefix_map[app_config.route_prefix] = app_meta.current
        headers_prefix_map[f"/{app_config.name}"] = app_meta.current
    parent.add_middleware(
        DeployHeadersMiddleware,
        manifests_by_prefix=headers_prefix_map,
        platform_manifest=platform_meta.current,
    )

    # Inject deploy <meta> tags into HTML responses so a browser can compare
    # its embedded SHA against /_meta (the stale-cache diagnostic). It rewrites
    # the body, so it must sit INSIDE the compression middleware (GZip): the
    # GZip add_middleware call below comes AFTER this one, making GZip outermost
    # so it compresses the already-injected HTML.
    parent.add_middleware(
        DeployMetaTagMiddleware,
        manifests_by_prefix=headers_prefix_map,
        platform_manifest=platform_meta.current,
    )

    # Give every app's HTML the platform's icon wiring (favicon, apple-touch-icon,
    # web-app manifest). Rewrites the body, so it too sits inside GZip.
    from enlace.app_icons import AppIconLinksMiddleware

    parent.add_middleware(
        AppIconLinksMiddleware,
        config=config,
        is_protected=lambda app: app.access.startswith("protected"),
    )

    # Count page views of the apps that opted in. It only observes status and
    # content type (nothing in the body), so its position is not load-bearing;
    # it sits inside GZip only so it sees the same responses the others do.
    if analytics is not None:
        analytics.add_middleware(parent)

    # Compress sizeable text/JSON responses. Added LAST so it is the OUTERMOST
    # middleware — it must wrap everything downstream (meta injection, sub-app
    # responses, static files) and see final bytes.
    #
    # NOT Starlette's GZipMiddleware, which compresses *everything* — including
    # `206 Partial Content`. That gzips a byte range while Content-Range still
    # describes the uncompressed representation, so the two headers disagree and
    # media seeking breaks (and Safari will not play <video> at all without
    # working ranges). It also spends real CPU, synchronously on the shared event
    # loop, gzipping video and images that are already compressed, for ~1% gain.
    # SelectiveGZipMiddleware skips ranged exchanges and incompressible content
    # types, and is otherwise identical. See gzip_selective.py.
    parent.add_middleware(SelectiveGZipMiddleware, minimum_size=1024)

    return parent


def _detect_enlace_version() -> Optional[str]:
    """Return the installed enlace version, or ``None`` if unknown."""
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _v

        return _v("enlace")
    except PackageNotFoundError:
        return None
    except Exception:  # pragma: no cover — defensive
        return None


#: How recent a manifest's mtime must be for its stat key not to be trusted yet:
#: comfortably more than a filesystem timestamp tick (a few ms on Linux).
_MTIME_TICK_NS = 50_000_000
#: A stat key no real one equals (an absent file keys as ``None``): read next time.
_UNTRUSTED = object()


class _MtimeCachedManifest:
    """One manifest as it is on disk *now*, re-read when the file changes.

    ``/_meta`` is a diagnostic endpoint, so a payload captured once at compose
    time is the wrong answer. Anything that legitimately rewrites the manifest
    while the backend runs — a connector refresh with its own unit, say — stays
    invisible until a restart, and nothing in the response distinguishes "still
    current" from "captured hours ago". See
    https://github.com/i2mint/enlace/issues/42.

    Reads are keyed on the manifest file's ``(mtime, size)``: self-invalidating,
    and one ``stat`` per request on the cache-hit path. With no manifest
    directory configured, or no file there, the compose-time manifest is served
    unchanged — which is exactly the previous behaviour.

    *name* selects the file (``{name}.json``): the platform manifest by default,
    or one app's. Each app gets its own reader, shared by its ``/_meta`` routes
    and the deploy-identity middlewares, so the endpoints and the headers can
    never disagree about the same app (#81).
    """

    def __init__(
        self,
        manifest: DeployManifest,
        *,
        name: str = PLATFORM_MANIFEST_NAME,
        manifest_dir: Optional[Path] = None,
        enlace_version: Optional[str] = None,
    ):
        self._manifest = manifest
        self._name = name
        self._manifest_dir = manifest_dir
        self._enlace_version = enlace_version
        self._path = None if manifest_dir is None else manifest_dir / f"{name}.json"
        # Untrusted until the first request, so the first read refreshes rather
        # than trusting a snapshot taken before the routes even existed.
        self._stat_key: object = _UNTRUSTED

    def payload(self) -> dict:
        """Return the manifest as a dict, plus the ``manifest_mtime`` it reflects.

        ``manifest_mtime`` is ISO-8601 UTC, or ``None`` when there is no manifest
        file to date the answer by — so a reader is never left guessing how old
        the payload is.
        """
        manifest, st = self._refresh()
        payload = manifest.model_dump()
        payload["manifest_mtime"] = None if st is None else _iso_utc(st.st_mtime)
        return payload

    def current(self) -> DeployManifest:
        """Return the manifest as it is on disk now (for the response headers)."""
        return self._refresh()[0]

    def _refresh(self) -> tuple[DeployManifest, Optional[os.stat_result]]:
        st = self._stat()
        stat_key = None if st is None else (st.st_ino, st.st_mtime_ns, st.st_size)
        if stat_key != self._stat_key:
            try:
                # Degrades to a stub whose ``extra.manifest_error`` says why on
                # any file it can parse as bad (see ``load_manifest``).
                self._manifest = load_manifest(
                    self._name, self._manifest_dir, enlace_version=self._enlace_version
                )
            except Exception:  # noqa: BLE001 — e.g. RecursionError on deep JSON
                # The deploy-identity middlewares call this on EVERY request under
                # the app's prefix, so it must never raise: keep the last manifest.
                _logger.warning("Unusable manifest %s", self._path, exc_info=True)
            # Filesystem mtimes are coarse (a kernel tick), so a second same-size
            # write within the tick of the first leaves the key unchanged. While
            # the file is that young, re-read it next time too ("racy git").
            young = st is not None and time.time_ns() - st.st_mtime_ns < _MTIME_TICK_NS
            self._stat_key = _UNTRUSTED if young else stat_key
        return self._manifest, st

    def _stat(self) -> Optional[os.stat_result]:
        """``stat`` the manifest file, or ``None`` if there is nothing to stat."""
        if self._path is None:
            return None
        try:
            return self._path.stat()
        except OSError:
            return None


def _iso_utc(timestamp: float) -> str:
    """Format a POSIX timestamp as ISO-8601 UTC, matching the manifest's own style."""
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _add_meta_routes(
    parent: FastAPI,
    config: PlatformConfig,
    platform_meta: "_MtimeCachedManifest",
    app_metas: dict[str, "_MtimeCachedManifest"],
    *,
    enlace_version: Optional[str] = None,
) -> None:
    """Mount /_meta (platform) and /{route_prefix}/_meta (per-app) endpoints.

    Two per-app paths are registered so the manifest is reachable both from
    the API mount (``{route_prefix}/_meta``) and the frontend mount
    (``/{name}/_meta``). Browser JS commonly fetches the latter with a
    relative URL from the SPA root.

    Every endpoint reads through a ``_MtimeCachedManifest``, so it reflects the
    manifest on disk now rather than a compose-time snapshot (#49 for the
    platform, #81 for the apps).
    """
    # Apps serving a 500 stub (enlace.degraded). Read per request, not
    # captured here: an app whose lifespan startup fails is stubbed after
    # these routes exist.
    from enlace.degraded import DEGRADED_APPS_KEY, DEGRADED_KEY, degraded_apps

    @parent.get("/_meta", include_in_schema=False)
    async def _platform_meta() -> dict:
        payload = platform_meta.payload()
        payload[RUNTIME_VERSION_KEY] = enlace_version
        degraded = degraded_apps(config)
        if degraded:
            payload[DEGRADED_APPS_KEY] = degraded
        return payload

    for app_config in config.apps:
        manifest = app_metas.get(app_config.name)
        if manifest is None:
            continue

        def extra(app_config=app_config) -> dict:
            out = {RUNTIME_VERSION_KEY: enlace_version}
            if app_config.import_error is not None:
                out[DEGRADED_KEY] = app_config.import_error.public_summary()
            return out

        _register_app_meta(
            parent, app_config.route_prefix + "/_meta", manifest, extra=extra
        )
        # Same data under the frontend mount so SPA code can fetch it with a
        # path relative to its own root.
        frontend_meta = f"/{app_config.name}/_meta"
        if frontend_meta != app_config.route_prefix + "/_meta":
            _register_app_meta(parent, frontend_meta, manifest, extra=extra)


def _register_app_meta(
    parent: FastAPI,
    path: str,
    manifest: "_MtimeCachedManifest",
    *,
    extra: Optional[Callable[[], dict]] = None,
) -> None:
    """Serve ``manifest`` as it is *now* at ``path``, plus what ``extra()`` says."""

    async def _meta() -> dict:
        return {**manifest.payload(), **(extra() if extra else {})}

    parent.add_api_route(path, _meta, methods=["GET"], include_in_schema=False)


def _add_trailing_slash_redirect(parent: FastAPI, prefix: str) -> None:
    """Redirect GET/HEAD /{prefix} → /{prefix}/ so mounted apps work either way.

    Starlette ``mount()`` only matches paths starting with ``prefix + "/"``,
    so a bare ``/{prefix}`` returns 404. This adds a sibling route that
    307-redirects to the trailing-slash form, matching how browsers commonly
    expect URLs to behave. HEAD is included so health checks and crawlers
    that probe with HEAD see the same routing as GET. State-changing methods
    (POST/PUT/DELETE) intentionally aren't redirected — API clients should
    hit the canonical trailing-slash path directly.

    No-op when ``prefix`` is empty or ``/`` (the root mount handles those).
    """
    if not prefix or prefix == "/":
        return

    async def _redirect() -> RedirectResponse:
        return RedirectResponse(url_path(f"{prefix}/"))

    parent.add_api_route(
        prefix,
        _redirect,
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )


def _add_cors(parent: FastAPI, origins: list[str]) -> None:
    """Install CORS per ``PlatformConfig.cors_origins``.

    Credentials are only ever allowed for an explicit origin list. With
    ``["*"]`` plus credentials, Starlette reflects *any* request origin and
    adds ``Access-Control-Allow-Credentials: true``, so a page on any origin
    that gets the browser to send the session cookie could read a signed-in
    user's responses. ``*`` therefore means "anyone, anonymously".
    """
    origins = list(origins or [])
    if not origins:
        return
    wildcard = "*" in origins
    parent.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if wildcard else origins,
        allow_credentials=not wildcard,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _add_router_root_redirects(parent: FastAPI) -> None:
    """Add trailing-slash redirects for router roots like ``/_admin/``.

    Plugins include routers at directory-style prefixes (e.g. enlace_auth's
    ``/_admin/``). Starlette serves only the trailing-slash form, and the
    catch-all ``/`` 404 mount shadows its built-in redirect — so a bare
    ``/_admin`` would 404. App and frontend prefixes are already handled at
    mount time via :func:`_add_trailing_slash_redirect`; this generalizes the
    same treatment to any remaining router root that lacks a no-slash sibling.
    """
    existing = {r.path for r in parent.routes if isinstance(r, Route)}
    for route in list(parent.routes):
        if not isinstance(route, Route) or route.path == "/":
            continue
        if route.path.endswith("/"):
            bare = route.path.rstrip("/")
            if bare and bare not in existing:
                _add_trailing_slash_redirect(parent, bare)
                existing.add(bare)


def _stamp_updated_at(
    config: PlatformConfig, manifests: dict[str, DeployManifest]
) -> None:
    """Stamp each app's "last updated" onto its config, from its deploy manifest.

    Prefers ``app_source.committed_at`` (when the source last actually *changed*)
    over ``deployed_at`` (when we last *shipped* it) — a full deploy writes the
    same ``deployed_at`` to every app, which would make the launcher's "last
    updated" sort a meaningless tie. ``committed_at`` is absent on manifests
    written before it existed, so the fallback keeps those apps sortable-ish
    rather than undated.

    Mutating the ``AppConfig`` (rather than passing manifests down) is deliberate:
    ``build_launcher_item`` is also called by the ``enlace_auth`` plugin after a
    metadata edit, and it holds these same config objects — so the field is
    carried there for free, and an edited app cannot come back missing its date.
    """
    for app in config.apps:
        manifest = manifests.get(app.name)
        if manifest is None:
            continue
        app.updated_at = _updated_at(manifest)


def _updated_at(manifest: DeployManifest) -> Optional[str]:
    """An app's "last updated", from its manifest -- see ``_stamp_updated_at``."""
    return manifest.app_source.committed_at or manifest.deployed_at


def refresh_updated_at(app: AppConfig, readers: Optional[Mapping[str, Any]]) -> None:
    """Re-stamp ``app.updated_at`` from its live manifest reader, if it has one.

    *readers* maps app names to objects with a ``current() -> DeployManifest``
    method: ``parent.state.app_manifest_readers``, which ``build_backend`` sets
    (one mtime-cached reader per app manifest). ``GET /_apps`` calls this for
    each listed app; a plugin that builds a launcher item itself (the
    ``enlace_auth`` metadata edit) calls it first, so its item carries the date
    in the file, not the date the last listing stamped (#87). An app without a
    reader, or no readers at all, keeps its current stamp, and so does one whose
    manifest cannot be read right now (corrupt or half-written): a known date is
    not replaced by "unknown" over a read error.
    """
    meta = (readers or {}).get(app.name)
    if meta is None:
        return
    manifest = meta.current()
    if MANIFEST_ERROR_KEY in (manifest.extra or {}):
        return
    app.updated_at = _updated_at(manifest)


def _can_access(app: AppConfig, request: Request) -> bool:
    """Whether the caller of ``request`` may see ``app`` in /_apps.

    Delegates to :func:`enlace.access.can_see_app` — the same allowlist
    predicate the auth gate calls — with the runtime grants the auth plugin
    injected (see :data:`enlace.access.GRANTS_STATE_ATTR`), resolved per
    request so a grant appears, and expires, in the launcher exactly when it
    does at the gate.
    """
    from enlace import access

    resolver = getattr(request.app.state, access.GRANTS_STATE_ATTR, None)
    granted = (
        access.granted_users(resolver, app.name)
        if app.access == "protected:user"
        else ()
    )
    return access.can_see_app(
        app.access,
        getattr(request.state, "user_id", None),
        getattr(request.state, "user_email", None),
        allowed_users=app.allowed_users,
        granted=granted,
    )


def _app_launch(app: AppConfig) -> tuple[bool, Optional[str]]:
    """Whether an app opens a human UI, and the URL to open.

    ``has_frontend`` is metadata, not the launch signal: an ``external`` proxy
    (typola) has no local frontend but serves a working UI at its route prefix,
    and a ``process`` connector (an MCP endpoint) has a route but no human page.
    An explicit ``app.toml`` ``launchable`` overrides the mode-derived default.
    An app that failed to load (it is serving a 500 stub) is never launchable,
    whatever its app.toml says.
    """
    if app.import_error is not None:
        return False, None
    has_frontend = bool(app.frontend_dir and app.frontend_dir.is_dir())
    if app.launchable is not None:
        launchable = app.launchable
    elif app.mode == "external":
        launchable = True
    elif app.mode == "process":
        launchable = False
    else:
        launchable = has_frontend
    if not launchable:
        return False, None
    if has_frontend:
        return True, url_path(f"/{app.name}/")  # the SPA mount, not route_prefix
    return True, url_path(app.route_prefix.rstrip("/") + "/")  # external proxy UI


def _overlay_entry(request: Request, name: str) -> dict:
    """Read one app's runtime overlay record (Tier A), or ``{}`` if none."""
    from enlace.app_icons import overlay_entry

    return overlay_entry(request.scope, name)


def _resolved_app_meta(app: AppConfig, config: PlatformConfig, overlay: dict) -> dict:
    """Resolve an app's launcher fields across Tier A (overlay) / B / C / D.

    Returns the display_name, description, keywords (+ per-tier sources), and
    icon_url for one ``/_apps`` item.
    """
    from enlace import app_icons, appmeta

    tier_b = config.app_meta.apps.get(app.name)
    b_name = tier_b.display_name if tier_b else None
    b_desc = tier_b.description if tier_b else None
    b_keywords = tier_b.keywords if tier_b else []

    display_name = overlay.get("display_name") or b_name or app.display_name
    description = overlay.get("description") or b_desc or app.description or ""

    keywords, sources = appmeta.resolve_keywords(
        app_keywords=app.keywords,
        platform_keywords=b_keywords,
        overlay_keywords=overlay.get("keywords", []),
    )

    # token_only: the listing needs only the ?v= cache token, not the icon bytes,
    # so a file-backed icon is stat'd, not read (27 apps × every /_apps hit).
    icon = app_icons.resolve_app_icon(app, config, overlay, token_only=True)
    return {
        "display_name": display_name,
        "description": description,
        "keywords": keywords,
        "keyword_sources": sources,
        "icon_url": f"{url_path(f'/_apps/{app.name}')}/icon?v={icon.token}",
    }


def build_launcher_item(app: AppConfig, config: PlatformConfig, overlay: dict) -> dict:
    """Build one ``/_apps`` item: resolved metadata + launchability, for an app.

    Public (unlike the ``_``-helpers) so the ``enlace_auth`` plugin's
    metadata-edit endpoints can return the *identical* item shape after a
    mutation — single source of truth for the item contract. ``overlay`` is the
    app's Tier-A record (``{}`` if none).

    The link fields (``route``, ``launch_url``, ``icon_url``) are percent-encoded
    URLs (#75); ``api_route`` is the configured route as an identifier, decoded,
    and must be passed through :func:`enlace.util.url_path` before use as a link.
    """
    launchable, launch_url = _app_launch(app)
    resolved = _resolved_app_meta(app, config, overlay)
    return {
        "name": app.name,
        "display_name": resolved["display_name"],
        "description": resolved["description"],
        "keywords": resolved["keywords"],
        "keyword_sources": resolved["keyword_sources"],
        "icon_url": resolved["icon_url"],
        "route": url_path(f"/{app.name}/"),  # back-compat; new landing ignores
        # The configured route as an identifier (not a link), so left decoded.
        "api_route": app.route_prefix,
        "access": app.access,
        "has_frontend": bool(app.frontend_dir and app.frontend_dir.is_dir()),
        "has_api": app.app_type != "frontend_only",
        "launchable": launchable,
        "launch_url": launch_url,
        # ISO-8601 or null — see _stamp_updated_at. The launcher sorts on it.
        "updated_at": app.updated_at,
        # True when the app failed to load and a 500 stub serves its routes
        # (enlace.degraded). The launcher shows it as broken, not launchable.
        "degraded": app.import_error is not None,
    }


def _add_apps_listing_route(
    parent: FastAPI,
    config: PlatformConfig,
    app_metas: Optional[dict[str, "_MtimeCachedManifest"]] = None,
) -> None:
    """Add GET /_apps (+ per-app icon route) for the launcher UI.

    ``/_apps`` returns the resolved launcher metadata for every app the caller
    can see; the landing app itself is hidden (it's the shell, not a listable
    app). ``/_apps/{name}/icon`` serves each app's icon through the platform so
    that even an auth-gated app's icon renders for an anonymous viewer who is
    allowed to *see* the app in the list. New fields are purely additive, so an
    older landing UI keeps working.

    Each listed app's ``updated_at`` is re-stamped from its *app_metas* reader
    on every request (one ``stat`` per app), so a manifest rewritten while the
    backend runs moves the launcher's "last updated" without a restart (#84).
    The stamp lands on the ``AppConfig``, so ``build_launcher_item`` callers
    elsewhere (the ``enlace_auth`` metadata edit) see it too.
    """
    app_metas = app_metas or {}
    apps = config.apps
    landing_name = config.landing_app
    apps_by_name = {a.name: a for a in apps}

    @parent.get("/_apps")
    async def apps_listing(request: Request) -> dict:
        user_id = getattr(request.state, "user_id", None)
        user_email = getattr(request.state, "user_email", None)
        can_edit_fn = getattr(request.app.state, "app_meta_can_edit", None)
        can_edit_meta = (
            bool(can_edit_fn(user_email)) if callable(can_edit_fn) else False
        )

        items = []
        for app in apps:
            if app.name == landing_name:
                continue
            if not _can_access(app, request):
                continue
            refresh_updated_at(app, app_metas)
            items.append(
                build_launcher_item(app, config, _overlay_entry(request, app.name))
            )
        return {
            "apps": items,
            "user": {"id": user_id, "email": user_email} if user_id else None,
            "can_edit_meta": can_edit_meta,
        }

    def _visible_app(name: str, request: Request) -> Optional[AppConfig]:
        """The named app if this caller may see it, else None.

        One uniform answer for unknown-or-forbidden, so the icon routes are
        never an oracle for apps the caller can't see.
        """
        app = apps_by_name.get(name)
        if app is None or app.name == landing_name:
            return None
        if not _can_access(app, request):
            return None
        return app

    @parent.get("/_apps/{name}/icon")
    async def app_icon(name: str, request: Request) -> Response:
        from enlace import app_icons

        app = _visible_app(name, request)
        if app is None:
            return Response(status_code=404)
        icon = app_icons.resolve_app_icon(app, config, _overlay_entry(request, name))
        if icon.redirect_url:
            # We don't control the remote bytes ⇒ never mark immutable.
            return RedirectResponse(
                icon.redirect_url,
                status_code=307,
                headers={"Cache-Control": "no-store"},
            )
        cache = "public, max-age=31536000, immutable" if icon.immutable else "no-store"
        # An icon may be an SVG (generated monogram/glyph, or an editor-supplied
        # file/data-URI). nosniff does NOT stop script in an SVG loaded as a
        # *document*; this CSP neutralises any embedded script if someone
        # navigates directly to the icon URL. Inert on the grid's <img> load,
        # which never executes SVG script anyway.
        return _icon_response(icon.body, icon.content_type, icon.token, cache)

    @parent.get("/_apps/{name}/icon-{size}.png")
    async def app_icon_png(name: str, size: int, request: Request) -> Response:
        """A square PNG of the app's icon — favicon, apple-touch-icon, manifest."""
        from enlace import app_icons

        app = _visible_app(name, request)
        if app is None or size not in app_icons.PNG_SIZES:
            return Response(status_code=404)
        icon = app_icons.resolve_app_icon(app, config, _overlay_entry(request, name))
        png = (
            app_icons.png_rendition(icon.body, size)
            if app_icons.is_raster(icon) and icon.body
            else None
        )
        if png is None:
            return Response(status_code=404)
        cache = "public, max-age=31536000, immutable" if icon.immutable else "no-store"
        return _icon_response(png, "image/png", f"{icon.token}-{size}", cache)

    @parent.get("/_apps/{name}/manifest.webmanifest")
    async def app_manifest(name: str, request: Request) -> Response:
        """The app's web-app manifest (Android "Add to Home screen" reads it)."""
        from enlace import app_icons

        app = _visible_app(name, request)
        if app is None:
            return Response(status_code=404)
        overlay = _overlay_entry(request, name)
        resolved = _resolved_app_meta(app, config, overlay)
        icon = app_icons.resolve_app_icon(app, config, overlay, token_only=True)
        manifest = app_icons.web_manifest(
            app,
            display_name=resolved["display_name"],
            description=resolved["description"],
            token=icon.token,
            has_png=app_icons.is_raster(icon),
            display=config.app_meta.manifest_display,
        )
        # Not immutable: the name or icon can change live through the overlay.
        return Response(
            content=app_icons.manifest_json(manifest),
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )


def _icon_response(body: bytes, content_type: str, token: str, cache: str) -> Response:
    """An icon response with the headers every icon route shares."""
    icon_csp = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return Response(
        content=body,
        media_type=content_type,
        headers={
            "ETag": f'"{token}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": icon_csp,
            "Cache-Control": cache,
        },
    )


def _add_index_route(parent: FastAPI, config: PlatformConfig) -> None:
    """Add a GET / route listing, as a simple HTML page, the apps the caller may see.

    Filtered by the same predicate as ``/_apps`` (:func:`_can_access`), so the
    built-in index never names an app the caller could not open.
    """

    @parent.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        apps = [a for a in config.apps if _can_access(a, request)]
        items = []
        for app in apps:
            has_frontend = app.frontend_dir and app.frontend_dir.is_dir()
            has_api = app.app_type != "frontend_only"
            links = []
            if has_frontend:
                links.append(f'<a href="{url_path(f"/{app.name}/")}">open</a>')
            if has_api:
                links.append(
                    f'<a href="{url_path(app.route_prefix)}/docs">api docs</a>'
                )
            link_html = " · ".join(links) if links else "(no routes)"
            if app.import_error is not None:
                link_html = "<em>unavailable: failed to load</em>"
            items.append(f"<li><strong>{app.display_name}</strong> — {link_html}</li>")
        app_list = "\n".join(items) if items else "<li>No apps discovered.</li>"
        return (
            "<!doctype html>"
            "<html><head><meta charset='utf-8'>"
            "<title>enlace</title>"
            "<style>"
            "body{font-family:system-ui,sans-serif;max-width:640px;"
            "margin:40px auto;padding:0 20px;color:#333}"
            "a{color:#2563eb} li{margin:8px 0}"
            "</style></head><body>"
            "<h1>enlace</h1>"
            f"<p>{len(apps)} app{'s' if len(apps) != 1 else ''} running:</p>"
            f"<ul>{app_list}</ul>"
            "</body></html>"
        )


class _AppIdInjector:
    """Pure-ASGI wrapper that stamps the app's name into scope state.

    Sub-apps never read this themselves — ``StoreInjectionMiddleware`` does,
    so it knows which app's per-user store to attach.
    """

    def __init__(self, app, app_id: str):
        self.app = app
        self.app_id = app_id

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            state = scope.setdefault("state", {})
            state["app_id"] = self.app_id
        await self.app(scope, receive, send)


def _load_sub_app(app_config: AppConfig) -> Optional[object]:
    """Import and return the sub-app for a given AppConfig.

    For asgi_app type: imports the module and returns the app attribute.
    For functions type: builds an APIRouter wrapping the module's public functions.

    Uses app_config.source_dir for correct sys.path resolution.
    """
    if app_config.entry_module_path is None:
        return None

    source_dir = app_config.source_dir
    if source_dir is None:
        # Fallback for manually-constructed AppConfigs without source_dir
        source_dir = app_config.entry_module_path.parent.parent

    module = _import_app_module(app_config.entry_module_path, source_dir)

    if app_config.app_type == "asgi_app":
        sub_app = getattr(module, app_config.app_attr, None)
        if sub_app is None:
            raise AttributeError(
                f"Module {app_config.entry_module_path} has no attribute "
                f"'{app_config.app_attr}'"
            )
        return sub_app

    elif app_config.app_type == "functions":
        return _build_router_from_functions(module, app_config)

    return None


def _build_router_from_functions(module, app_config: AppConfig) -> FastAPI:
    """Build a FastAPI sub-app from a module's public typed functions."""
    sub_app = FastAPI(title=app_config.display_name)

    for name, func in inspect.getmembers(module, inspect.isfunction):
        if name.startswith("_") or func.__module__ != module.__name__:
            continue
        # Use GET for no-param functions, POST for functions with params
        sig = inspect.signature(func)
        params = [p for p in sig.parameters.values() if p.name not in ("self", "cls")]
        method = "GET" if not params else "POST"
        sub_app.add_api_route(f"/{name}", func, methods=[method])

    return sub_app


def _import_app_module(entry_path: Path, source_dir: Path):
    """Import the app module from a filesystem path.

    Uses source_dir.parent as the sys.path root. The path is added
    permanently (needed for the lifetime of the server process).
    """
    root = str(source_dir.parent.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    relative = entry_path.resolve().relative_to(Path(root).resolve())
    module_name = str(relative.with_suffix("")).replace("/", ".").replace("\\", ".")
    if module_name in sys.modules:
        del sys.modules[module_name]
    with forget_partial_imports(module_name):
        return importlib.import_module(module_name)


def create_app() -> FastAPI:
    """App factory for Uvicorn's --factory flag.

    Loads platform config, discovers apps, checks conflicts,
    and builds the composed backend.

    Discovery *records* an app whose entry module fails to import rather than
    raising, so ``build_backend`` can mount a 500 stub for that app alone
    (see :mod:`enlace.degraded`). Platform configuration errors, including a
    broken ``ENLACE_PLUGINS`` entry, still raise here.

    Plugins are loaded from the ``ENLACE_PLUGINS`` env var: a comma-separated
    list of ``module:attribute`` pairs, e.g.::

        ENLACE_PLUGINS=enlace_auth:plugin

    Each resolved object must be a callable
    ``(parent: FastAPI, config: PlatformConfig) -> None``.
    """
    config = discover_apps(on_import_error="record")
    config = _apply_port_env(config)
    plugins = _load_plugins_from_env()
    return build_backend(config, plugins=plugins)


def _load_plugins_from_env() -> list[Plugin]:
    """Parse ENLACE_PLUGINS=mod:attr,mod2:attr2 and resolve to callables."""
    raw = os.environ.get("ENLACE_PLUGINS", "").strip()
    if not raw:
        return []
    out: list[Plugin] = []
    for spec in (s.strip() for s in raw.split(",")):
        if not spec:
            continue
        if ":" not in spec:
            raise EnlaceConfigError(
                f"ENLACE_PLUGINS entry {spec!r} is not in 'module:attribute' form"
            )
        mod_name, attr = spec.split(":", 1)
        try:
            mod = importlib.import_module(mod_name)
        except ImportError as e:
            raise EnlaceConfigError(
                f"ENLACE_PLUGINS: cannot import {mod_name!r}: {e}"
            ) from e
        try:
            obj = getattr(mod, attr)
        except AttributeError as e:
            raise EnlaceConfigError(
                f"ENLACE_PLUGINS: {mod_name!r} has no attribute {attr!r}"
            ) from e
        if not callable(obj):
            raise EnlaceConfigError(
                f"ENLACE_PLUGINS: {spec!r} resolved to non-callable {obj!r}"
            )
        out.append(obj)
    return out


def _apply_port_env(config: PlatformConfig) -> PlatformConfig:
    """Apply auto-allocated ports from the ENLACE_PROCESS_PORTS env var.

    When serve.py runs in mixed mode, it auto-allocates ports for
    ``needs_port`` apps and stores them in an env var so the gateway
    subprocess can read them and set up correct proxy routes. The map only
    ever contains needs_port apps, so we apply by name without re-checking
    the mode here (which keeps this open to plugin strategies).
    """
    import json
    import os

    raw = os.environ.get("ENLACE_PROCESS_PORTS", "")
    if not raw:
        return config
    try:
        port_map = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return config

    updated_apps = []
    for app in config.apps:
        if app.name in port_map:
            port = int(port_map[app.name])
            app = app.model_copy(update={"port": port})
        updated_apps.append(app)

    return config.model_copy(update={"apps": updated_apps})
