"""Serve an app's page from its Python package: the shell and its ``/ui/`` assets.

Some apps ship their whole page inside their Python package: a script and a
stylesheet (and maybe vendored files) under a data directory, plus a tiny HTML
shell that tells the script where its API lives. Before this module, the host
committed that generated shell as the app's ``frontend/index.html`` and the app
served its own assets from a hand-written ``/ui/{name}`` route. Now one app.toml
key does both, and a UI change ships by upgrading the package::

    page_from_package = "mypkg"     # assets in mypkg/data/ui, generic shell

    [page_from_package]             # or the full table
    package = "songleaf"
    assets = "data/web"             # directory inside the package
    shell = "songleaf.web:page_shell"   # the app's own shell, if it has one

What enlace serves for such an app (``name``, API at ``route_prefix``):

- ``/{name}/`` and every path under it: the shell (client-side routing), with
  ``Cache-Control: no-cache`` and an ``ETag``, so a package upgrade reaches
  the browser on its next load.
- ``{route_prefix}/ui/<file>``: the files of the package's assets directory,
  always revalidated. Mounted ahead of the app; what it does not serve falls
  through to the app, so the app's own ``/ui`` routes keep working.

The shell is the app's own (``shell = "module:function"``, called as
``function(api=..., base=..., title=...)`` and returning HTML) or enlace's
generic one (:func:`page_shell`): ``window.<GLOBAL> = {api, base, title}`` and
the ``scripts`` and ``stylesheets``, loaded from the assets route.

The app does not change and does not import enlace. Run alone, it keeps
serving its own page however it already did.

The package is imported into the gateway, as an ``asgi`` app is. A failure to
import it, find its assets directory or build the shell is a load failure of
the app (see :mod:`enlace.degraded`): loud, and local to that app.
"""

import hashlib
import html
import importlib
import json
import re
from dataclasses import dataclass, field
from importlib.resources import files as package_files
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

#: Where the assets live inside the package, by default.
DFLT_ASSETS_DIR = "data/ui"
#: The route segment, under the app's API prefix, the assets are served at.
DFLT_ASSETS_ROUTE = "ui"
DFLT_SCRIPTS = ("app.js",)
DFLT_STYLESHEETS = ("style.css",)
#: Cache policy of the shell and the assets: keep, but revalidate (a cheap 304).
DFLT_CACHE_CONTROL = "no-cache"

_JS_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_SHELL_REF = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_]\w*")
_SAFE_METHODS = frozenset({"GET", "HEAD"})
#: What the assets app hands to the app's own mount instead of answering.
_FALLBACK_STATUSES = frozenset({404, 405})

#: ``(*, api, base, title) -> html``: what a shell function is called with.
ShellFunc = Callable[..., str]


class PackagePageConfig(BaseModel):
    """app.toml's ``page_from_package``: the package that ships the app's page.

    A bare string is the package name, every other field at its default.

    >>> PackagePageConfig.model_validate("songleaf").assets
    'data/ui'
    >>> PackagePageConfig(package="my_pkg.sub").global_name
    'MY_PKG'
    """

    model_config = ConfigDict(extra="forbid")

    package: str = Field(description="Importable package that ships the page")
    assets: str = Field(
        default=DFLT_ASSETS_DIR,
        description="Directory inside the package holding the page's files",
    )
    assets_route: str = Field(
        default=DFLT_ASSETS_ROUTE,
        description="Route segment under the app's API prefix the files are served at",
    )
    files: list[str] = Field(
        default_factory=list,
        description="Allowlist of servable files (relative paths); empty: all of them",
    )
    shell: Optional[str] = Field(
        default=None,
        description=(
            "'module:function' building the shell, called with keyword arguments "
            "api, base and title; default: enlace's generic shell"
        ),
    )
    global_name: str = Field(
        default="",
        description=(
            "Generic shell: the window property holding {api, base, title}; "
            "default: the package's top-level name, upper-cased"
        ),
    )
    title: str = Field(default="", description="Page title; default: the app's")
    scripts: list[str] = Field(default_factory=lambda: list(DFLT_SCRIPTS))
    stylesheets: list[str] = Field(default_factory=lambda: list(DFLT_STYLESHEETS))

    @model_validator(mode="before")
    @classmethod
    def _from_package_name(cls, value):
        return {"package": value} if isinstance(value, str) else value

    @field_validator("assets", "assets_route")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        parts = [p for p in value.strip("/").split("/") if p]
        if not parts or any(p in (".", "..") for p in parts):
            raise ValueError(f"must be a relative path inside the package: {value!r}")
        return "/".join(parts)

    @field_validator("shell")
    @classmethod
    def _shell_ref(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _SHELL_REF.fullmatch(value):
            raise ValueError(f"shell is 'module:function', got {value!r}")
        return value

    @model_validator(mode="after")
    def _default_global_name(self):
        if not self.global_name:
            top = self.package.split(".")[0]
            self.global_name = re.sub(r"\W", "_", top).upper()
        if self.files and not self.shell:
            allowed = {name.strip("/") for name in self.files}
            missing = [
                name
                for name in [*self.scripts, *self.stylesheets]
                if name.strip("/") not in allowed
            ]
            if missing:
                raise ValueError(
                    f"the shell loads {missing}, which files (the allowlist) leaves out"
                )
        if not _JS_IDENTIFIER.fullmatch(self.global_name):
            raise ValueError(
                f"global_name must be a JavaScript identifier: {self.global_name!r}"
            )
        return self


def page_shell(
    *,
    api: str,
    base: str = "",
    title: str = "",
    global_name: str = "APP",
    scripts: tuple[str, ...] = DFLT_SCRIPTS,
    stylesheets: tuple[str, ...] = DFLT_STYLESHEETS,
    assets_route: str = DFLT_ASSETS_ROUTE,
) -> str:
    """The generic shell: ``window.<global_name> = {api, base, title}``, then assets.

    ``api`` is where the app's API is mounted (its files are under
    ``{api}/{assets_route}/``), ``base`` where the page lives.

    >>> shell = page_shell(api="/api/demo", base="/demo", title="Demo")
    >>> '<script src="/api/demo/ui/app.js"></script>' in shell
    True
    >>> 'window.APP = {"api": "/api/demo", "base": "/demo", "title": "Demo"}' in shell
    True
    """
    if not _JS_IDENTIFIER.fullmatch(global_name):
        raise ValueError(
            f"global_name must be a JavaScript identifier: {global_name!r}"
        )
    config = {"api": api.rstrip("/"), "base": base.rstrip("/"), "title": title}
    # "<" escaped, so no "</script>" in a value can close the script element
    config_js = json.dumps(config).replace("<", "\\u003c")
    assets_url = f"{config['api']}/{assets_route.strip('/')}/"

    def url(name: str) -> str:
        return html.escape(assets_url + name.lstrip("/"))

    links = "".join(f'<link rel="stylesheet" href="{url(s)}">\n' for s in stylesheets)
    tags = "".join(f'<script src="{url(s)}"></script>\n' for s in scripts)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{html.escape(title)}</title>
<script>window.{global_name} = {config_js};</script>
{links}</head>
<body>
<div id="app" aria-live="polite"></div>
{tags}</body>
</html>
"""


@dataclass(frozen=True)
class PackagePage:
    """A resolved package page: the shell's HTML and the directory of its assets."""

    shell: str
    assets_dir: Path
    assets_route: str = DFLT_ASSETS_ROUTE
    files: tuple[str, ...] = field(default_factory=tuple)

    def shell_app(self) -> "ShellApp":
        """The ASGI app serving the shell at every path under the page's mount."""
        return ShellApp(self.shell)

    def assets_app(self, *, fallback: Optional[ASGIApp] = None) -> "PackageAssets":
        """The ASGI app serving the assets directory; misses go to ``fallback``."""
        return PackageAssets(
            directory=str(self.assets_dir),
            allowed=self.files,
            fallback=fallback,
            route=self.assets_route,
        )


def resolve_package_page(
    config: PackagePageConfig, *, api: str, base: str, title: str
) -> PackagePage:
    """Import the package, find its assets directory and build the shell.

    Raises (``ImportError``, ``FileNotFoundError``, ``TypeError``...) when the
    package, its assets directory or the shell function is not usable; the
    caller decides whether that stubs the app or stops the boot.
    """
    assets_dir = _assets_dir(config.package, config.assets)
    title = config.title or title
    if config.shell:
        shell = _shell_func(config.shell)(api=api, base=base, title=title)
        if not isinstance(shell, str):
            raise TypeError(
                f"{config.shell} returned {type(shell).__name__}, not an HTML str"
            )
    else:
        shell = page_shell(
            api=api,
            base=base,
            title=title,
            global_name=config.global_name,
            scripts=tuple(config.scripts),
            stylesheets=tuple(config.stylesheets),
            assets_route=config.assets_route,
        )
    return PackagePage(
        shell=shell,
        assets_dir=assets_dir,
        assets_route=config.assets_route,
        files=tuple(config.files),
    )


def _assets_dir(package: str, assets: str) -> Path:
    """The package's assets directory, as a directory on disk."""
    resource = package_files(package)
    for part in assets.split("/"):  # one at a time: 3.10's MultiplexedPath takes one
        resource = resource.joinpath(part)
    path = Path(str(resource))
    if not path.is_dir():
        raise FileNotFoundError(
            f"package {package!r} has no directory {assets!r} (looked for {path}); "
            "set page_from_package.assets to where its page files are"
        )
    return path


def _shell_func(ref: str) -> ShellFunc:
    module_name, _, attr = ref.partition(":")
    func = getattr(importlib.import_module(module_name), attr, None)
    if not callable(func):
        raise AttributeError(f"{ref}: {module_name!r} has no function {attr!r}")
    return func


class ShellApp:
    """Pure-ASGI app answering every GET/HEAD with the shell (client-side routing).

    ``Cache-Control: no-cache`` plus a strong ``ETag``, so a browser revalidates
    with a cheap 304 and picks up a new shell after a package upgrade.
    """

    def __init__(self, shell: str, *, cache_control: str = DFLT_CACHE_CONTROL):
        self._body = shell.encode("utf-8")
        self._etag = '"' + hashlib.sha256(self._body).hexdigest()[:32] + '"'
        self._cache_control = cache_control

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await receive()  # websocket.connect
                await send({"type": "websocket.close", "code": 1000})
            return
        method = scope.get("method", "GET").upper()
        if method not in _SAFE_METHODS:
            response = Response(
                "Method Not Allowed", status_code=405, headers={"Allow": "GET, HEAD"}
            )
        elif _etag_matches(self._etag, scope):
            response = Response(status_code=304, headers=self._headers())
        else:
            response = Response(
                self._body,
                media_type="text/html; charset=utf-8",
                headers=self._headers(),
            )
        await response(scope, receive, send)

    def _headers(self) -> dict[str, str]:
        return {"Cache-Control": self._cache_control, "ETag": self._etag}


def _etag_matches(etag: str, scope: Scope) -> bool:
    """Whether ``If-None-Match`` names ``etag`` (weak comparison, ``*`` matches)."""
    for key, value in scope.get("headers", ()):
        if key == b"if-none-match":
            tags = {tag.strip() for tag in value.decode("latin-1").split(",")}
            return "*" in tags or etag in {t.removeprefix("W/") for t in tags}
    return False


class PackageAssets(StaticFiles):
    """A package's page files, always revalidated, optionally allowlisted.

    Hidden files (a path segment starting with ``.``) and ``__pycache__`` are
    never served. A request it does not serve (no such file, not on the
    allowlist, a method other than GET/HEAD, a websocket) goes to
    ``fallback`` when one is given (the app's own mount, so a route the app
    has under the same segment keeps working), else gets a 404 / 405.
    ``fallback`` sees the scope the app's mount would have given it: the
    assets route segment moves from ``root_path`` back into the path.
    """

    def __init__(
        self,
        *args,
        allowed: tuple[str, ...] = (),
        cache_control: str = DFLT_CACHE_CONTROL,
        fallback: Optional[ASGIApp] = None,
        route: str = DFLT_ASSETS_ROUTE,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.allowed = frozenset(name.strip("/") for name in allowed)
        self.cache_control = cache_control
        self.fallback = fallback
        self._route_suffix = "/" + route.strip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.fallback is None:
            await super().__call__(scope, receive, send)
            return
        if scope["type"] == "http":
            if not self.config_checked:
                await self.check_config()
                self.config_checked = True
            try:
                response = await self.get_response(self.get_path(scope), scope)
            except HTTPException as exc:
                if exc.status_code not in _FALLBACK_STATUSES:
                    raise
                response = None
            if response is not None and response.status_code not in (
                _FALLBACK_STATUSES
            ):
                await response(scope, receive, send)
                return
        await self.fallback(self._fallback_scope(scope), receive, send)

    def _fallback_scope(self, scope: Scope) -> Scope:
        root_path = scope.get("root_path", "")
        if root_path.endswith(self._route_suffix):
            root_path = root_path[: -len(self._route_suffix)]
        return {**scope, "root_path": root_path}

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Refuse hidden files and, with an allowlist, any file not on it."""
        rel = path.replace("\\", "/")
        segments = rel.split("/")
        hidden = any(seg.startswith(".") or seg == "__pycache__" for seg in segments)
        if hidden or (self.allowed and rel not in self.allowed):
            return Response("Not Found", status_code=404)
        return await super().get_response(path, scope)

    def file_response(self, *args, **kwargs) -> Response:
        """The file, with ``Cache-Control`` so a package upgrade reaches the browser."""
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = self.cache_control
        return response
