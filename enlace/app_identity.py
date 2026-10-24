"""The app identity checklist: what every public-facing app's page must carry.

One list, stated once (:data:`CHECKLIST`), checked in one place
(:func:`identity_gaps`) and reported by every surface that audits an app
(``enlace check``, ``enlace doctor``, a deploy gate reading the doctor's JSON).

The platform already wires the favicon, apple-touch-icon and web manifest
``<link>`` tags from the app's icon (see :mod:`enlace.app_icons`), so those need
only a raster source icon here. What the app's own page must supply is its
``<head>`` text and social-preview tags.

>>> sorted(CHECKLIST)[:3]
['description', 'icon-512', 'icon-png']
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from enlace.app_icons import has_own_pages, is_raster, resolve_app_icon
from enlace.appmeta import _HeadParser

#: Smallest PNG source that renders crisply at the largest manifest size.
MIN_ICON_PX = 512

#: code -> what the app owes (the SSOT; docs and skills point here).
CHECKLIST: dict[str, str] = {
    "title": "a <title> in the page <head>",
    "description": '<meta name="description"> (or description in app.toml)',
    "theme-color": '<meta name="theme-color">',
    "og-image": '<meta property="og:image"> (social preview PNG)',
    "icon-png": "a raster icon (platform cuts favicon, apple-touch 180)",
    "icon-512": f"a PNG source icon >= {MIN_ICON_PX}px wide (manifest 512)",
}


#: How much of index.html to parse: the <head> is at the top, and a page with
#: megabytes of inline script or data must still be judged on its head.
_HEAD_BYTES = 256 * 1024


def _page_head(app) -> Optional[_HeadParser]:
    try:
        with (Path(app.frontend_dir) / "index.html").open("rb") as f:
            text = f.read(_HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    parser = _HeadParser()
    try:
        parser.feed(text)
    except Exception:  # a malformed document is a gap, not a crash
        return None
    return parser


def app_gaps(app, config) -> list[str]:
    """Checklist codes this one app misses (empty: complete)."""
    head = _page_head(app)
    gaps = []
    if not (head and head.title.strip()):
        gaps.append("title")
    tier_b = config.app_meta.apps.get(app.name)
    described = app.description or (tier_b and tier_b.description)
    if not (described or (head and (head.description or head.og_description))):
        gaps.append("description")
    if not (head and head.theme_color):
        gaps.append("theme-color")
    if not (head and head.og_image):
        gaps.append("og-image")
    if app.name != config.landing_app:
        icon = resolve_app_icon(app, config, {})
        if icon.redirect_url:
            pass  # a remote icon cannot be inspected here
        elif not is_raster(icon):
            gaps.append("icon-png")
        elif _icon_px(icon) < MIN_ICON_PX:
            gaps.append("icon-512")
    return gaps


def _icon_px(icon) -> int:
    """Pixel width of a raster icon (PNG header, else Pillow; 0 if unreadable)."""
    px = _png_px(icon.body)
    if px or icon.content_type == "image/png":
        return px
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(icon.body or b"")) as im:
            return im.size[0]
    except Exception:  # no Pillow, or not decodable: treat as unknown
        return MIN_ICON_PX


def _png_px(body: Optional[bytes]) -> int:
    head = (body or b"")[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n" or len(head) < 24:
        return 0
    return int.from_bytes(head[16:20], "big")


def identity_gaps(config) -> dict[str, list[str]]:
    """``{app name: missing codes}`` for apps whose HTML the platform serves."""
    out = {}
    for app in config.apps:
        if not has_own_pages(app) or not getattr(app, "listed", True):
            continue
        gaps = app_gaps(app, config)
        if gaps:
            out[app.name] = gaps
    return out
