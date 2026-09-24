"""One icon per app, served in every form a browser or phone asks for.

An app's icon appears in four places: the launcher grid, the browser tab
(favicon), the iOS home screen (``apple-touch-icon``, PNG only) and the Android
/ Chrome home screen (the web-app manifest's ``icons``). Left to each app, those
four drift apart — most apps declare none of them, and the few that do declare
different subsets. So the platform owns them, once:

- **Source** — :func:`icon_source` picks one icon spec per app, in tier order:
  runtime overlay (Tier A) > ``[app_meta.apps.<name>].icon`` (Tier B) > a file
  named ``<name>.<ext>`` in ``[app_meta].icons_dir`` (Tier B, by convention) >
  the app's own declared/harvested icon (Tier C) > ``default_icon`` > monogram.
- **Renditions** — ``/_apps/{name}/icon`` serves the source as-is;
  ``/_apps/{name}/icon-{size}.png`` serves square PNGs (:data:`PNG_SIZES`) cut
  from a *raster* source; ``/_apps/{name}/manifest.webmanifest`` is generated.
- **Wiring** — :class:`AppIconLinksMiddleware` adds the ``<link>`` tags an app's
  HTML is missing. When the icon comes from the platform tiers (A/B), the page's
  own icon links are replaced, so the owner's choice wins everywhere; otherwise
  the app's own links stand and only the gaps are filled.

PNG renditions need Pillow and a raster source (PNG/JPEG/WebP/GIF/ICO). A
vector-only icon (SVG, emoji glyph, monogram) still serves as the favicon, but
has no PNG form — so no home-screen icon. Give such an app a PNG in
``icons_dir``; :func:`home_screen_gaps` lists them.
"""

from __future__ import annotations

import html
import io
import json
import re
from pathlib import Path
from typing import Optional

from enlace.html_rewrite import HEAD_CLOSE_RE, inject_into_head, rewrite_html_response

# favicon (32), apple-touch-icon (180), manifest (192, 512 — Chrome's install bar).
PNG_SIZES = (32, 180, 192, 512)
APPLE_TOUCH_SIZE = 180
MANIFEST_SIZES = (192, 512)

_RASTER_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/x-icon",
    "image/vnd.microsoft.icon",
}
_ICONS_DIR_EXTS = (".png", ".webp", ".jpg", ".jpeg", ".svg")

# <link rel="icon" …>, <link rel="shortcut icon" …>, <link rel="apple-touch-icon…" …>
_ICON_LINK_RE = re.compile(
    rb"<link\b[^>]*\brel\s*=\s*[\"']?(?:shortcut\s+)?(?:icon|apple-touch-icon(?:-precomposed)?)"
    rb"[\"'\s>][^>]*>\s*",
    re.IGNORECASE,
)
_MANIFEST_LINK_RE = re.compile(
    rb"<link\b[^>]*\brel\s*=\s*[\"']?manifest[\"'\s>]", re.IGNORECASE
)
_APPLE_TITLE_RE = re.compile(
    rb"<meta\b[^>]*\bname\s*=\s*[\"']?apple-mobile-web-app-title", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Source: which icon an app has
# ---------------------------------------------------------------------------


def icons_dir_file(icons_dir: Optional[Path], name: str) -> Optional[str]:
    """The filename of ``<name>.<ext>`` in the platform icons dir, if present.

    Raster formats are probed before SVG, so an app with both gets PNG
    renditions (the SVG can sit beside it as the editable source).
    """
    if icons_dir is None:
        return None
    for ext in _ICONS_DIR_EXTS:
        if (icons_dir / f"{name}{ext}").is_file():
            return f"{name}{ext}"
    return None


def icon_source(app, config, overlay: dict) -> tuple[str, Optional[Path], bool]:
    """Return ``(spec, root, platform_owned)`` for one app's icon.

    ``root`` is the directory a file spec is resolved (and contained) under.
    ``platform_owned`` is True when the icon comes from the platform tiers
    (overlay, ``[app_meta.apps]``, ``icons_dir``) rather than the app itself —
    the signal that the page's own icon links should be replaced.
    """
    from enlace.appmeta import app_dir_of

    meta = config.app_meta
    tier_b = meta.apps.get(app.name)
    app_dir = app_dir_of(app)
    if overlay.get("icon"):
        return overlay["icon"], app_dir, True
    if tier_b and tier_b.icon:
        return tier_b.icon, app_dir, True
    in_dir = icons_dir_file(meta.icons_dir, app.name)
    if in_dir:
        return in_dir, meta.icons_dir, True
    return (app.icon or meta.default_icon or ""), app_dir, False


def display_name_of(app, config, overlay: dict) -> str:
    """The app's display name across Tier A / B / C (same order as ``/_apps``)."""
    tier_b = config.app_meta.apps.get(app.name)
    return (
        overlay.get("display_name")
        or (tier_b.display_name if tier_b else None)
        or app.display_name
    )


def resolve_app_icon(app, config, overlay: dict, *, token_only: bool = False):
    """Resolve an app's icon to an :class:`~enlace.appmeta.IconResult`."""
    from enlace.appmeta import resolve_icon

    spec, root, _ = icon_source(app, config, overlay)
    return resolve_icon(
        spec,
        app_name=app.name,
        display_name=display_name_of(app, config, overlay),
        app_dir=root,
        token_only=token_only,
    )


def is_raster(icon) -> bool:
    """Whether an icon can be cut into PNG renditions (a raster, served inline)."""
    return not icon.redirect_url and icon.content_type in _RASTER_TYPES


# ---------------------------------------------------------------------------
# Renditions
# ---------------------------------------------------------------------------


def png_rendition(body: bytes, size: int) -> Optional[bytes]:
    """A ``size``×``size`` PNG cut from raster ``body``, or None if impossible.

    Non-square sources are center-cropped (a home-screen tile is square, and
    padding would add a frame the artwork never had). Transparency is kept for
    the favicon but flattened onto white for the larger sizes, because iOS
    renders a transparent apple-touch-icon's clear pixels as black.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(body)) as im:
            if getattr(im, "n_frames", 1) > 1:
                im.seek(0)
            im = im.convert("RGBA")
            w, h = im.size
            side = min(w, h)
            left, top = (w - side) // 2, (h - side) // 2
            im = im.crop((left, top, left + side, top + side))
            im = im.resize((size, size), Image.LANCZOS)
            if size >= APPLE_TOUCH_SIZE:
                flat = Image.new("RGBA", im.size, (255, 255, 255, 255))
                flat.alpha_composite(im)
                im = flat.convert("RGB")
            out = io.BytesIO()
            im.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except Exception:  # a corrupt icon file must 404, never 500
        return None


def web_manifest(
    app, *, display_name: str, description: str, token: str, has_png: bool, display: str
) -> dict:
    """The web-app manifest for one app (what Android's "Add to Home screen" reads).

    Scoped to the app's own mount so each app installs as its own shortcut.
    """
    base = f"/_apps/{app.name}"
    if has_png:
        icons = [
            {
                "src": f"{base}/icon-{s}.png?v={token}",
                "sizes": f"{s}x{s}",
                "type": "image/png",
                "purpose": "any",
            }
            for s in MANIFEST_SIZES
        ]
    else:
        icons = [{"src": f"{base}/icon?v={token}", "sizes": "any"}]
    manifest = {
        "name": display_name,
        "short_name": display_name,
        "start_url": f"/{app.name}/",
        "scope": f"/{app.name}/",
        "display": display,
        "icons": icons,
    }
    if description:
        manifest["description"] = description
    return manifest


def home_screen_gaps(config) -> list[str]:
    """Names of launchable apps whose icon has no PNG form (no home-screen icon)."""
    gaps = []
    for app in config.apps:
        if app.name == config.landing_app or not has_own_pages(app):
            continue
        icon = resolve_app_icon(app, config, {}, token_only=True)
        if not is_raster(icon):
            gaps.append(app.name)
    return gaps


def has_own_pages(app) -> bool:
    """Whether the platform serves this app's HTML itself (so it can wire its head)."""
    return bool(app.frontend_dir and Path(app.frontend_dir).is_dir())


# ---------------------------------------------------------------------------
# Wiring: the <head> links
# ---------------------------------------------------------------------------


def _tag(tag: str, **attrs: str) -> str:
    """An HTML start tag with escaped attribute values (``crossorigin`` et al.)."""
    parts = " ".join(f'{k}="{html.escape(v, quote=True)}"' for k, v in attrs.items())
    return f"<{tag} {parts}>"


_HAS_ICON_RE = re.compile(rb"\brel\s*=\s*[\"']?(?:shortcut\s+)?icon[\"'\s>]", re.I)
_HAS_APPLE_RE = re.compile(rb"apple-touch-icon", re.I)


def head_links(
    app, *, icon, display_name: str, protected: bool, page_head: bytes, replace: bool
) -> tuple[bytes, bytes]:
    """Return ``(snippet, head)``: tags to add, and the head minus replaced ones.

    Adds each of favicon / apple-touch-icon / manifest / home-screen title only
    where the page lacks it — unless ``replace``, in which case the page's own
    icon links are dropped and the platform's take their place.
    """
    base = f"/_apps/{app.name}"
    v = icon.token
    raster = is_raster(icon)
    head = _ICON_LINK_RE.sub(b"", page_head) if replace else page_head
    tags: list[str] = []
    if not _HAS_ICON_RE.search(head):
        if raster:
            for s in (32, 192):
                href = f"{base}/icon-{s}.png?v={v}"
                size = f"{s}x{s}"
                tags.append(
                    _tag("link", rel="icon", type="image/png", sizes=size, href=href)
                )
        else:
            tags.append(_tag("link", rel="icon", href=f"{base}/icon?v={v}"))
    if raster and not _HAS_APPLE_RE.search(head):
        href = f"{base}/icon-{APPLE_TOUCH_SIZE}.png?v={v}"
        tags.append(_tag("link", rel="apple-touch-icon", sizes="180x180", href=href))
    if not _MANIFEST_LINK_RE.search(head):
        # A manifest is fetched WITHOUT cookies unless asked; a protected app's
        # manifest route needs them to pass its access check.
        cred = {"crossorigin": "use-credentials"} if protected else {}
        href = f"{base}/manifest.webmanifest"
        tags.append(_tag("link", rel="manifest", href=href, **cred))
    if not _APPLE_TITLE_RE.search(head):
        title = _tag("meta", name="apple-mobile-web-app-title", content=display_name)
        tags.append(title)
    return "".join(tags).encode("utf-8"), head


def _split_head(body: bytes) -> tuple[bytes, bytes]:
    """Split an HTML body at ``</head>`` (head part may be the whole doc)."""
    m = HEAD_CLOSE_RE.search(body)
    if not m:
        return body, b""
    return body[: m.start()], body[m.start() :]


class AppIconLinksMiddleware:
    """Pure-ASGI middleware giving every app's HTML the same icon wiring.

    For ``text/html`` responses under ``/{app}/``, see :func:`head_links`. The
    overlay (Tier A) is read per request from ``app.state.app_meta_overlay``, so
    a live icon edit in the launcher reaches tabs and home screens with no
    redeploy. Must sit inside compression (it edits the body).
    """

    def __init__(self, app, *, config, is_protected):
        self.app = app
        self._config = config
        self._is_protected = is_protected
        landing = config.landing_app
        self._apps = sorted(
            (
                (f"/{a.name}/", a)
                for a in config.apps
                if a.name != landing and has_own_pages(a)
            ),
            key=lambda kv: -len(kv[0]),
        )

    def _app_for(self, path: str):
        for prefix, app in self._apps:
            if path.startswith(prefix):
                return app
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        app = self._app_for(scope.get("path", ""))
        if app is None:
            await self.app(scope, receive, send)
            return

        overlay = overlay_entry(scope, app.name)

        def rewrite(body: bytes) -> bytes:
            try:
                icon = resolve_app_icon(app, self._config, overlay, token_only=True)
                _, _, replace = icon_source(app, self._config, overlay)
                head, rest = _split_head(body)
                snippet, head = head_links(
                    app,
                    icon=icon,
                    display_name=display_name_of(app, self._config, overlay),
                    protected=self._is_protected(app),
                    page_head=head,
                    replace=replace,
                )
                return inject_into_head(head + rest, snippet)
            except Exception:  # icon wiring must never break a page
                return body

        await rewrite_html_response(self.app, scope, receive, send, rewrite)


def overlay_entry(scope, name: str) -> dict:
    """Read one app's runtime overlay record (Tier A), or ``{}`` if none.

    ``app_meta_overlay`` is injected on the root app's state by the
    ``enlace_auth`` plugin; absent it, core degrades to Tiers B/C/D.
    """
    parent = scope.get("app")
    store = getattr(getattr(parent, "state", None), "app_meta_overlay", None)
    if store is None:
        return {}
    try:
        rec = store.get(name, {})
    except Exception:  # a flaky store must never break a page
        return {}
    return rec if isinstance(rec, dict) else {}


def manifest_json(manifest: dict) -> bytes:
    """Serialize a manifest (kept apart so routes and tests share one encoding)."""
    return json.dumps(manifest, ensure_ascii=False).encode("utf-8")
