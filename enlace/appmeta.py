"""App metadata: harvest, resolve, and render titles / descriptions / keywords / icons.

This module gives every discovered app a *title*, a *description*, a set of
*keywords*, and an *icon* — assembled from the standard places an app already
declares that information, so that a launcher UI can present a searchable grid
of apps without each app having to be hand-registered.

Two things live here:

1. **Harvest + resolve** (`harvest_app_metadata`) — at *discovery* time, read the
   app-declared metadata from, in precedence order, ``app.toml`` → a Web App
   Manifest → the ``index.html`` ``<head>`` → ``package.json`` → ``pyproject.toml``
   → filesystem icon conventions, and bake the resolved values onto the
   ``AppConfig``. Scalars are first-non-empty-wins; keywords are *unioned* across
   every source (so app-declared keywords accumulate rather than overwrite).

2. **Icon rendering** (`resolve_icon`) — at *request* time, turn an app's icon
   spec (an emoji, a relative image path, a ``data:`` URI, an absolute URL, or
   nothing) into bytes to serve (or a redirect). An unset icon falls back to a
   deterministic, font-guaranteed letter *monogram* on a name-hashed gradient, so
   a grid of many apps reads as one coherent system rather than a wall of
   placeholders.

Layering note: this module is pure read-model. The *editable* overlay (a
runtime store of owner-added keywords / icon overrides) and its HTTP write
surface live in the ``enlace_auth`` plugin, not here — enlace core never mutates
metadata. Core only *reads* an injected overlay mapping via ``request.app.state``
(see ``enlace.compose``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

try:  # Python 3.11+ has tomllib; fall back to tomli for the pyproject harvester.
    import tomllib
except ImportError:  # pragma: no cover - exercised only on <3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Config models (Tier B: platform.toml [app_meta])
# ---------------------------------------------------------------------------


class AppMetaEntry(BaseModel):
    """Per-app static override from ``platform.toml`` (``[app_meta.apps.<name>]``).

    The platform owner's static layer, applied on top of app-declared metadata.
    ``keywords`` here are *added* to the app's own (union), not replaced; the
    scalar fields override first-non-empty-wins if set.
    """

    display_name: Optional[str] = None
    description: Optional[str] = None
    keywords: list[str] = Field(default_factory=list)
    icon: Optional[str] = None


class AppMetaConfig(BaseModel):
    """The ``[app_meta]`` table of ``platform.toml``.

    ``default_icon`` and ``apps`` are consumed by enlace core (read model).
    ``editors`` and ``store_path`` are *held but not acted on* by core — the
    ``enlace_auth`` plugin reads them to gate and persist the runtime overlay,
    mirroring how ``PlatformConfig.auth`` / ``.stores`` are carried for the
    plugin without core interpreting them.
    """

    default_icon: str = ""
    apps: dict[str, AppMetaEntry] = Field(default_factory=dict)
    editors: list[str] = Field(default_factory=list)
    store_path: Optional[Path] = None


# ---------------------------------------------------------------------------
# Harvesting (Tier C — app-declared, resolved at discovery)
# ---------------------------------------------------------------------------

# Read-size cap for any harvested source file: enough for a real <head> or
# manifest, small enough that a pathological file can't stall discovery.
_MAX_HARVEST_BYTES = 256 * 1024

# Web App Manifest filenames, in the order we probe for them.
_MANIFEST_NAMES = ("manifest.webmanifest", "manifest.json", "site.webmanifest")

# Filesystem icon conventions (relative to an app / frontend dir), best first.
_ICON_CONVENTIONS = (
    "icon.svg",
    "icon.png",
    "favicon.svg",
    "assets/favicon-96.png",
    "assets/favicon-32.png",
    "apple-touch-icon.png",
    "assets/apple-touch-icon.png",
    "favicon.ico",
)


@dataclass
class _Harvest:
    """Accumulator for one source's contribution to an app's metadata.

    ``keywords`` union across sources; the scalars are filled first-non-empty.
    ``icon`` is stored as a path/spec relative to the *app dir* so the icon
    endpoint can resolve it uniformly, regardless of which source found it.
    """

    display_name: str = ""
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    icon: str = ""


def _norm_keywords(values) -> list[str]:
    """Normalize a keyword iterable: strip, drop blanks, dedupe casefold-wise.

    Keeps the first-seen surface form and a stable order — so a UI shows
    "Cheap Flights" (as authored) but ``cheap flights`` won't appear twice.
    """
    out: list[str] = []
    seen: set[str] = set()
    for v in values or ():
        if not isinstance(v, str):
            continue
        s = v.strip()
        if not s:
            continue
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _read_text_capped(path: Path) -> Optional[str]:
    """Read a text file up to the harvest cap; ``None`` on any error/oversize path.

    Never raises — a malformed or unreadable source must degrade to "absent",
    never break discovery (which would take down every route).
    """
    try:
        if not path.is_file():
            return None
        with path.open("rb") as f:
            raw = f.read(_MAX_HARVEST_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_HARVEST_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _rel_to_app(app_dir: Path, target: Path) -> str:
    """Return ``target`` as a POSIX path relative to ``app_dir``, or "" if outside.

    Icon specs are always stored app-dir-relative so a single icon endpoint can
    resolve them; an icon that resolves outside the app dir is discarded here.
    """
    try:
        return target.resolve().relative_to(app_dir.resolve()).as_posix()
    except (ValueError, OSError):
        return ""


def _from_app_dir_manifest(app_dir: Path, frontend_dir: Optional[Path]) -> _Harvest:
    """Harvest from a Web App Manifest (the W3C standard for exactly this data)."""
    h = _Harvest(keywords=[])
    search_dirs = [d for d in (frontend_dir, app_dir) if d is not None]
    for d in search_dirs:
        for name in _MANIFEST_NAMES:
            text = _read_text_capped(d / name)
            if text is None:
                continue
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            h.display_name = str(
                data.get("name") or data.get("short_name") or ""
            ).strip()
            h.description = str(data.get("description") or "").strip()
            cats = data.get("categories")
            if isinstance(cats, list):
                h.keywords = _norm_keywords(cats)
            icon_rel = _best_manifest_icon(data.get("icons"), d, app_dir)
            if icon_rel:
                h.icon = icon_rel
            return h
    return h


def _best_manifest_icon(icons, manifest_dir: Path, app_dir: Path) -> str:
    """Pick the largest declared manifest icon, as an app-dir-relative path."""
    if not isinstance(icons, list):
        return ""
    best_src, best_area = "", -1
    for entry in icons:
        if not isinstance(entry, dict):
            continue
        src = entry.get("src")
        if not isinstance(src, str) or not src:
            continue
        sizes = str(entry.get("sizes", ""))
        # A scalable icon (sizes="any" or a .svg src) is preferred over any
        # fixed raster — treat it as maximal area. Strict `>` keeps the first
        # (declaration-order) winner on ties rather than the last.
        if "any" in sizes.lower() or src.lower().endswith(".svg"):
            area = 1 << 62
        else:
            m = re.search(r"(\d+)x(\d+)", sizes)
            area = int(m.group(1)) * int(m.group(2)) if m else 0
        if area > best_area:
            best_area, best_src = area, src
    if not best_src:
        return ""
    return _rel_to_app(app_dir, manifest_dir / best_src)


class _HeadParser(HTMLParser):
    """Extract title/description/keywords/icon signals from an HTML ``<head>``.

    Stops collecting once ``</head>`` (or ``<body>``) is seen — we never need
    the body, and bailing early bounds the work on a large document.
    """

    def __init__(self) -> None:
        super().__init__()
        self.done = False
        self._in_title = False
        self.title = ""
        self.application_name = ""
        self.apple_title = ""
        self.og_title = ""
        self.description = ""
        self.og_description = ""
        self.keywords = ""
        self.icon_href = ""
        self.apple_icon_href = ""

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag == "body":
            self.done = True
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = a.get("name", "").lower()
            prop = a.get("property", "").lower()
            content = a.get("content", "").strip()
            if not content:
                return
            if name == "application-name":
                self.application_name = content
            elif name == "apple-mobile-web-app-title":
                self.apple_title = content
            elif name == "description":
                self.description = content
            elif name == "keywords":
                self.keywords = content
            elif prop == "og:title":
                self.og_title = content
            elif prop == "og:description":
                self.og_description = content
        elif tag == "link":
            rel = a.get("rel", "").lower()
            href = a.get("href", "").strip()
            if not href:
                return
            if "apple-touch-icon" in rel:
                self.apple_icon_href = href
            elif "icon" in rel and not self.icon_href:
                self.icon_href = href

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data):
        if self._in_title and not self.done:
            self.title += data


def _from_html_head(app_dir: Path, frontend_dir: Optional[Path]) -> _Harvest:
    """Harvest from the served ``index.html`` ``<head>`` (browser/SEO metadata).

    Prefers clean title sources (``application-name`` / ``apple-mobile-web-app-title``)
    over ``<title>`` because the latter often carries suffixes ("— Papp").
    """
    h = _Harvest(keywords=[])
    if frontend_dir is None:
        return h
    text = _read_text_capped(frontend_dir / "index.html")
    if text is None:
        return h
    p = _HeadParser()
    try:
        p.feed(text)
    except Exception:  # a malformed doc must never break discovery
        return h
    h.display_name = (
        p.application_name or p.apple_title or p.og_title or p.title.strip()
    )
    h.description = p.description or p.og_description
    h.keywords = _norm_keywords(re.split(r"[,\n]", p.keywords)) if p.keywords else []
    href = p.icon_href or p.apple_icon_href
    if href and not _looks_remote(href):
        # hrefs are relative to the html file's directory (the frontend dir).
        h.icon = _rel_to_app(app_dir, frontend_dir / href.lstrip("./"))
    return h


def _from_package_json(app_dir: Path, frontend_dir: Optional[Path]) -> _Harvest:
    """Harvest name/description/keywords from a ``package.json`` (dev metadata)."""
    h = _Harvest(keywords=[])
    for d in (app_dir, frontend_dir):
        if d is None:
            continue
        text = _read_text_capped(d / "package.json")
        if text is None:
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        # A scoped/prefixed package name ("tw-platform-landing") is a poor title;
        # take it only as a last resort and humanize it.
        raw_name = str(data.get("name") or "").strip()
        if raw_name and not raw_name.startswith("@"):
            h.display_name = raw_name.replace("-", " ").replace("_", " ").strip()
        h.description = str(data.get("description") or "").strip()
        kw = data.get("keywords")
        if isinstance(kw, list):
            h.keywords = _norm_keywords(kw)
        return h
    return h


def _from_pyproject(app_dir: Path, frontend_dir: Optional[Path]) -> _Harvest:
    """Harvest description/keywords from ``pyproject.toml`` ``[project]``."""
    h = _Harvest(keywords=[])
    if tomllib is None:
        return h
    text_path = app_dir / "pyproject.toml"
    try:
        if not text_path.is_file() or text_path.stat().st_size > _MAX_HARVEST_BYTES:
            return h
        with text_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return h
    project = data.get("project") if isinstance(data, dict) else None
    if not isinstance(project, dict):
        return h
    h.description = str(project.get("description") or "").strip()
    kw = project.get("keywords")
    if isinstance(kw, list):
        h.keywords = _norm_keywords(kw)
    return h


def _from_fs_conventions(app_dir: Path, frontend_dir: Optional[Path]) -> _Harvest:
    """Harvest an icon by filesystem convention only (no title/keywords)."""
    h = _Harvest(keywords=[])
    search_dirs = [d for d in (frontend_dir, app_dir) if d is not None]
    for d in search_dirs:
        for rel in _ICON_CONVENTIONS:
            candidate = d / rel
            try:
                if candidate.is_file():
                    resolved = _rel_to_app(app_dir, candidate)
                    if resolved:
                        h.icon = resolved
                        return h
            except OSError:
                continue
    return h


# Harvesters in Tier-C precedence order (C.2 .. C.6). C.1 (app.toml) is applied
# upstream in discover.py via _CORE_TOML_FIELD_MAP, so the AppConfig already
# carries app.toml values with provenance before we run.
_HARVESTERS = (
    _from_app_dir_manifest,  # C.2
    _from_html_head,  # C.3
    _from_package_json,  # C.4
    _from_pyproject,  # C.5
    _from_fs_conventions,  # C.6 (icon only)
)


def harvest_app_metadata(config, app_dir: Path, frontend_dir: Optional[Path]) -> dict:
    """Resolve app-declared metadata for one app; return an ``AppConfig`` update dict.

    Called at the tail of discovery for *both* the asgi and non-asgi paths, so
    external/process apps (which have no frontend on disk) are covered too. Every
    harvester is a no-op when its source file is missing, so an app with only an
    ``app.toml`` — or nothing — resolves cleanly without raising.

    Precedence (first non-empty wins for scalars; keywords union across all):
      Tier C.1 app.toml (already on ``config``, gated by provenance for the title)
      Tier C.2 manifest → C.3 <head> → C.4 package.json → C.5 pyproject → C.6 fs
      Tier D   derived (title = auto-derived name; icon = "" ⇒ monogram at render)

    Returns only the fields that changed, suitable for ``model_copy(update=...)``.
    """
    harvests = [h(app_dir, frontend_dir) for h in _HARVESTERS]

    updates: dict = {}
    provenance = dict(getattr(config, "provenance", {}) or {})

    # --- title (display_name) ------------------------------------------------
    # The model pre-fills display_name from the app name unconditionally, so we
    # can't read the field to know whether app.toml declared a title. Use
    # provenance as the gate: only "override: app.toml" counts as C.1. Otherwise
    # fall through the harvest chain, and if nothing is found keep the
    # auto-derived default (reclassified as Tier D).
    title_from_toml = provenance.get("display_name") == "override: app.toml"
    if not title_from_toml:
        for h, src in zip(harvests, ("manifest", "html-head", "package.json")):
            if h.display_name:
                updates["display_name"] = h.display_name
                provenance["display_name"] = f"harvest: {src}"
                break

    # --- description (first non-empty; app.toml already on config) -----------
    if not getattr(config, "description", ""):
        for h, src in zip(
            harvests, ("manifest", "html-head", "package.json", "pyproject")
        ):
            if h.description:
                updates["description"] = h.description
                provenance["description"] = f"harvest: {src}"
                break

    # --- keywords (union: app.toml + every harvested source) -----------------
    merged_keywords = list(getattr(config, "keywords", []) or [])
    for h in harvests:
        merged_keywords.extend(h.keywords)
    merged_keywords = _norm_keywords(merged_keywords)
    if merged_keywords != list(getattr(config, "keywords", []) or []):
        updates["keywords"] = merged_keywords
        if merged_keywords:
            provenance.setdefault("keywords", "harvest: union")

    # --- icon (first non-empty; app.toml already on config) ------------------
    if not getattr(config, "icon", ""):
        for h, src in zip(
            harvests, ("manifest", "html-head", "package.json", "pyproject", "fs")
        ):
            if h.icon:
                updates["icon"] = h.icon
                provenance["icon"] = f"harvest: {src}"
                break

    if updates:
        updates["provenance"] = provenance
    return updates


# ---------------------------------------------------------------------------
# Icon rendering (request-time)
# ---------------------------------------------------------------------------

# Content types we will serve or redirect to. Anything else ⇒ treat the icon as
# unset and fall through to the monogram (never serve an arbitrary file type).
_ICON_CONTENT_TYPES = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
}
_ALLOWED_MEDIATYPES = set(_ICON_CONTENT_TYPES.values())

# Cap on a served icon file / decoded data URI (icons are small; a "logo" that
# is megabytes is misconfiguration, not an icon).
_MAX_ICON_BYTES = 512 * 1024

# Anything longer than this with no path separator is treated as a path attempt,
# not a glyph. A couple of codepoints (emoji + skin tone + ZWJ, or "λ") fit well
# under this; "assets/x.png" has a "/" and is excluded regardless.
_MAX_GLYPH_LEN = 12


@dataclass
class IconResult:
    """The outcome of resolving an icon spec.

    Exactly one of (``body``, ``redirect_url``) is set. ``token`` is a short
    content hash used as the ``?v=`` cache-buster and the ``ETag``. ``immutable``
    is False only for the redirect form (whose target we don't control).
    """

    content_type: str
    token: str
    body: Optional[bytes] = None
    redirect_url: Optional[str] = None
    immutable: bool = True


def _looks_remote(spec: str) -> bool:
    return spec.startswith(("http://", "https://", "//"))


def _short_hash(*parts: bytes) -> str:
    hasher = hashlib.sha256()
    for p in parts:
        hasher.update(p)
        hasher.update(b"\0")
    return hasher.hexdigest()[:16]


def app_dir_of(config) -> Optional[Path]:
    """Reconstruct an app's own directory from its ``AppConfig``.

    ``source_dir`` is the *container* dir and ``name`` the subdir, for both the
    walked-container and individual-app-dir discovery paths — so ``source_dir /
    name`` is the app dir in both cases.
    """
    src = getattr(config, "source_dir", None)
    name = getattr(config, "name", None)
    if src is None or not name:
        return None
    return Path(src) / name


def effective_icon_spec(
    config, *, overlay_icon: Optional[str] = None, default_icon: str = ""
) -> str:
    """The icon spec to render, applying Tier A (overlay) over C/D, then default.

    Empty string means "no explicit icon" — the renderer produces a monogram.
    """
    if overlay_icon:
        return overlay_icon
    if getattr(config, "icon", ""):
        return config.icon
    return default_icon or ""


def resolve_icon(
    spec: str,
    *,
    app_name: str,
    display_name: str,
    app_dir: Optional[Path],
    token_only: bool = False,
) -> IconResult:
    """Turn an icon spec into servable bytes (or a redirect) plus a cache token.

    Dispatch order: explicit ``emoji:`` → ``data:`` URI → absolute https URL
    (redirect) → contained image file → bare glyph → monogram fallback. Any spec
    that fails its form's validation falls through to the monogram, so the
    endpoint never errors and never serves an unexpected file type.

    ``token_only=True`` skips reading file bytes (the ``?v=`` token for a file is
    stat-derived, so it matches the full resolve either way). ``/_apps`` uses it
    to avoid reading every app's icon file on every listing; the icon endpoint
    uses the full form to get the bytes to serve.
    """
    spec = (spec or "").strip()

    if spec.startswith("emoji:"):
        return _render_glyph(spec[len("emoji:") :].strip(), app_name=app_name)

    if spec.startswith("data:"):
        result = _render_data_uri(spec)
        if result is not None:
            return result
        return _render_monogram(app_name=app_name, display_name=display_name)

    if _looks_remote(spec):
        if spec.startswith("https://"):
            return IconResult(
                content_type="",
                token=_short_hash(b"redirect", spec.encode("utf-8")),
                redirect_url=spec,
                immutable=False,
            )
        return _render_monogram(app_name=app_name, display_name=display_name)

    if spec and "/" not in spec and "\\" not in spec:
        # Ambiguous: a bare token could be a file ("logo.png") or a glyph ("λ").
        # Prefer a real contained file (needs app_dir); else, if it's a short
        # non-extension token, treat it as a glyph — glyphs need no filesystem.
        if app_dir is not None:
            served = _render_file(spec, app_dir=app_dir, token_only=token_only)
            if served is not None:
                return served
        if len(spec) <= _MAX_GLYPH_LEN and "." not in spec:
            return _render_glyph(spec, app_name=app_name)

    elif spec and app_dir is not None:
        # Has a path separator ⇒ only a file path makes sense here.
        served = _render_file(spec, app_dir=app_dir, token_only=token_only)
        if served is not None:
            return served

    return _render_monogram(app_name=app_name, display_name=display_name)


def _render_file(
    spec: str, *, app_dir: Path, token_only: bool = False
) -> Optional[IconResult]:
    """Serve a contained image file, or ``None`` if it's not a safe image path.

    Containment mirrors ``frontend.SPAStaticFiles._is_real_file``: reject ``..``
    segments, ``resolve()`` (which also collapses symlinks), then require the
    result to stay under the app dir and be a real file with an allowed
    extension.

    The cache token is derived from the file's ``(content_type, mtime, size)``,
    not its bytes — so ``/_apps`` can compute every app's ``?v=`` from a cheap
    ``stat`` (``token_only=True``) instead of reading 27 files on every hit,
    while still busting the cache when a file is edited or replaced in place.
    ``ValueError`` is caught alongside ``OSError`` because a NUL byte in the spec
    raises it — that must fall through to the monogram, never 500.
    """
    if ".." in spec.replace("\\", "/").split("/"):
        return None
    try:
        candidate = (app_dir / spec).resolve()
        candidate.relative_to(app_dir.resolve())
    except (OSError, ValueError):
        return None
    content_type = _ICON_CONTENT_TYPES.get(candidate.suffix.lower())
    if content_type is None:
        return None
    try:
        if not candidate.is_file():
            return None
        st = candidate.stat()
        if st.st_size > _MAX_ICON_BYTES:
            return None
        token = _short_hash(
            b"file",
            content_type.encode(),
            str(st.st_mtime_ns).encode(),
            str(st.st_size).encode(),
        )
        body = None if token_only else candidate.read_bytes()
    except (OSError, ValueError):
        return None
    return IconResult(content_type=content_type, token=token, body=body)


def _render_data_uri(spec: str) -> Optional[IconResult]:
    """Decode a ``data:image/...;base64,...`` URI to bytes, or ``None`` if invalid."""
    m = re.match(r"^data:([-\w.+/]+)(;base64)?,(.*)$", spec, re.DOTALL)
    if not m:
        return None
    mediatype, is_b64, payload = m.group(1).lower(), bool(m.group(2)), m.group(3)
    if mediatype not in _ALLOWED_MEDIATYPES:
        return None
    try:
        body = base64.b64decode(payload) if is_b64 else payload.encode("utf-8")
    except (binascii.Error, ValueError):
        return None
    if not body or len(body) > _MAX_ICON_BYTES:
        return None
    token = _short_hash(b"data", mediatype.encode(), body)
    return IconResult(content_type=mediatype, token=token, body=body)


def _hue_for(name: str) -> int:
    """Deterministic hue (0..359) from a name — stable across processes.

    Uses sha256, not the salted builtin ``hash``, so the same app gets the same
    color on every restart and every machine.
    """
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:2], "big") % 360


def _svg_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _gradient_svg(inner: str, *, name: str, grad_id: str) -> bytes:
    """Wrap ``inner`` SVG markup in a full-bleed name-hashed two-stop gradient.

    Saturation/lightness are fixed constants so hues stay harmonious across a
    grid; only the hue varies per app. Corner rounding is left to the tile CSS.
    """
    hue = _hue_for(name)
    c1 = f"hsl({hue}, 62%, 52%)"
    c2 = f"hsl({hue}, 62%, 42%)"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
        f'width="100" height="100" role="img">'
        f'<defs><linearGradient id="{grad_id}" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{c1}"/>'
        f'<stop offset="1" stop-color="{c2}"/></linearGradient></defs>'
        f'<rect width="100" height="100" fill="url(#{grad_id})"/>'
        f"{inner}</svg>"
    )
    return svg.encode("utf-8")


def _initials(display_name: str, app_name: str) -> str:
    """1–2 uppercase initials for a monogram, robust to odd names."""
    source = display_name or app_name or "?"
    words = [w for w in re.split(r"[\s_\-]+", source) if w]
    letters = ""
    if len(words) >= 2:
        letters = words[0][0] + words[1][0]
    elif words:
        w = re.sub(r"[^A-Za-z0-9]", "", words[0]) or words[0]
        letters = w[:2] if len(w) >= 2 else w[:1]
    return (letters or "?").upper()


def _render_monogram(*, app_name: str, display_name: str) -> IconResult:
    """A font-guaranteed letter monogram on a name-hashed gradient (the default)."""
    initials = _svg_escape(_initials(display_name, app_name))
    inner = (
        f'<text x="50" y="50" font-family="system-ui, -apple-system, '
        f'Segoe UI, Roboto, sans-serif" font-size="44" font-weight="700" '
        f'fill="#ffffff" text-anchor="middle" dominant-baseline="central">'
        f"{initials}</text>"
    )
    body = _gradient_svg(inner, name=app_name, grad_id="g")
    token = _short_hash(b"monogram", body)
    return IconResult(content_type="image/svg+xml", token=token, body=body)


def _render_glyph(glyph: str, *, app_name: str) -> IconResult:
    """An emoji/symbol glyph centered on the app's gradient (opt-in accent).

    The glyph is rendered as SVG ``<text>`` — the viewer's own color-emoji font
    draws it, so we don't rasterize on the server. Empty glyph ⇒ monogram.
    """
    glyph = (glyph or "").strip()
    if not glyph:
        return _render_monogram(app_name=app_name, display_name="")
    inner = (
        f'<text x="50" y="52" font-size="56" text-anchor="middle" '
        f'dominant-baseline="central">{_svg_escape(glyph)}</text>'
    )
    body = _gradient_svg(inner, name=app_name, grad_id="g")
    token = _short_hash(b"glyph", glyph.encode("utf-8"), body)
    return IconResult(content_type="image/svg+xml", token=token, body=body)


# ---------------------------------------------------------------------------
# Keyword resolution across tiers (request-time, for /_apps)
# ---------------------------------------------------------------------------


def resolve_keywords(
    *,
    app_keywords,
    platform_keywords,
    overlay_keywords,
) -> tuple[list[str], dict]:
    """Union app / platform / overlay keywords; return (merged, sources).

    ``merged`` is what search matches on. ``sources`` *partitions* ``merged``:
    each keyword appears in exactly one bucket — the first (highest-precedence)
    tier that declared it, in order app → platform → overlay. This keeps the
    UI's read-only-vs-editable split unambiguous: a keyword the app already
    declares stays in the read-only ``app`` bucket even if the owner also typed
    it into the overlay, so it isn't offered as a removable overlay chip
    (removing it wouldn't change ``merged`` anyway).
    """
    sources: dict[str, list[str]] = {"app": [], "platform": [], "overlay": []}
    seen: set[str] = set()
    for tier, values in (
        ("app", app_keywords),
        ("platform", platform_keywords),
        ("overlay", overlay_keywords),
    ):
        for kw in _norm_keywords(values):
            key = kw.casefold()
            if key in seen:
                continue
            seen.add(key)
            sources[tier].append(kw)
    merged = [*sources["app"], *sources["platform"], *sources["overlay"]]
    return merged, sources
