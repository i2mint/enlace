"""Deploy manifest: build-identity for diagnosing "what is actually deployed".

A deploy manifest answers two diagnostic questions that are otherwise hard to
answer about a running deployment:

1. Is the server really serving the SHA I think it is, or has the checkout
   drifted? (Including externals — editable installs of sibling packages.)
2. Is the page my browser rendered the **current** build, or is the browser
   serving a stale cache?

enlace owns the **schema** and the **read paths** (an HTTP endpoint per app,
a platform-level endpoint, response headers). The **write path** is the
deploy tool's responsibility: snapshot identity at deploy time and drop a
``{manifest_dir}/{app}.json`` file enlace can read. Snapshotting at deploy
time is load-bearing — it captures *what was actually deployed*, not whatever
the working tree on the server happens to say at startup.

The schema is versioned (``schema_version``) so future tooling that consumes
manifests across many apps can evolve without ambiguity.

The endpoint and header layer are always-on and cheap (a few hundred bytes
per app, one route per app, one short header tuple per response). They're
hidden from end users by default — apps can opt to surface the data visibly
(footer chip, About modal, devtools log) on top of the primitive.

See https://github.com/i2mint/enlace/issues/18 for design rationale.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

_logger = logging.getLogger("enlace.manifest")

MANIFEST_SCHEMA_VERSION = 1
PLATFORM_MANIFEST_NAME = "_platform"


class SourceRef(BaseModel):
    """Git identity for a single source tree (app or platform)."""

    sha: Optional[str] = None
    ref: Optional[str] = None
    dirty: Optional[bool] = None
    local_path: Optional[str] = None
    # When this source last actually CHANGED (ISO-8601; the committer date of the
    # newest commit touching it). Distinct from the manifest's ``deployed_at``,
    # which is when we last *shipped* it — a full deploy stamps every app with the
    # same instant, so it cannot order apps by recency. This can, which is what the
    # launcher's "last updated" sort needs. Optional: an app outside git has none.
    committed_at: Optional[str] = None


class ExternalRef(BaseModel):
    """Identity for an externally-installed dependency (e.g. an editable sibling)."""

    sha: Optional[str] = None
    version: Optional[str] = None


class DeployManifest(BaseModel):
    """What was deployed for one app (or the platform itself).

    ``app_source`` and ``platform_source`` use the same shape so consumers can
    treat them uniformly. ``externals`` is a free-form map keyed by package
    name — the manifest format is intentionally permissive about which keys
    appear, since which externals matter varies by app.
    """

    schema_version: int = MANIFEST_SCHEMA_VERSION
    app: str
    deployed_at: Optional[str] = None
    deployer: Optional[Literal["local", "ci"]] = None
    platform: Optional[str] = None
    app_source: SourceRef = Field(default_factory=SourceRef)
    platform_source: SourceRef = Field(default_factory=SourceRef)
    externals: dict[str, ExternalRef] = Field(default_factory=dict)
    enlace_version: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)


def resolve_manifest_dir(
    config_manifest_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Resolve the manifest directory from env var or config.

    ``ENLACE_MANIFEST_DIR`` takes precedence over the config value. Returns
    ``None`` when neither is set — callers treat that as "no on-disk manifests
    available; serve minimal stubs".
    """
    env = os.environ.get("ENLACE_MANIFEST_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return config_manifest_dir


def _read_manifest_file(path: Path) -> Optional[dict]:
    try:
        with open(path, "rb") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        # Don't let a corrupt manifest crash the server — log and degrade.
        _logger.warning("Failed to read manifest %s: %s", path, e)
        return None


def load_manifest(
    app_name: str,
    manifest_dir: Optional[Path],
    *,
    enlace_version: Optional[str] = None,
) -> DeployManifest:
    """Load the deploy manifest for one app, or return a minimal stub.

    The stub has just ``app`` (the name we were asked about) and
    ``enlace_version`` filled in. That keeps the diagnostic plumbing working
    even before any deploy tool starts writing manifests.
    """
    if manifest_dir is not None:
        path = manifest_dir / f"{app_name}.json"
        data = _read_manifest_file(path)
        if data is not None:
            data.setdefault("app", app_name)
            if enlace_version is not None and not data.get("enlace_version"):
                data["enlace_version"] = enlace_version
            return DeployManifest.model_validate(data)
    return DeployManifest(app=app_name, enlace_version=enlace_version)


def load_platform_manifest(
    manifest_dir: Optional[Path],
    *,
    enlace_version: Optional[str] = None,
) -> DeployManifest:
    """Load the platform-level deploy manifest (or a minimal stub)."""
    return load_manifest(
        PLATFORM_MANIFEST_NAME,
        manifest_dir,
        enlace_version=enlace_version,
    )


class _PrefixManifestMiddleware:
    """Base for middlewares that pick a manifest by longest-matching prefix.

    Both the header and meta-tag middlewares resolve which app a request
    belongs to the same way: match the request path against registered
    prefixes (API mount + frontend mount), longest first, falling back to
    the platform manifest. This base holds that shared selection so the two
    middlewares don't duplicate it.
    """

    def __init__(
        self,
        app,
        *,
        manifests_by_prefix: dict[str, DeployManifest],
        platform_manifest: Optional[DeployManifest] = None,
    ):
        self.app = app
        # Sort longest-first so /api/foo wins over /api when both registered.
        self._prefixes = sorted(manifests_by_prefix.items(), key=lambda kv: -len(kv[0]))
        self._platform = platform_manifest

    def _select(self, path: str) -> Optional[DeployManifest]:
        for prefix, manifest in self._prefixes:
            if path == prefix or path.startswith(prefix + "/"):
                return manifest
        return self._platform


class DeployHeadersMiddleware(_PrefixManifestMiddleware):
    """Pure-ASGI middleware that adds X-Deploy-* headers on every response.

    The header values come from the manifest matching the longest registered
    prefix (route_prefix or frontend mount path) for the request path. If no
    per-app prefix matches, the platform manifest fills in. Headers are only
    added when the underlying value is present — a stub manifest produces a
    short header set (just ``X-Deploy-App``), not bogus SHAs.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        manifest = self._select(scope.get("path", ""))
        if manifest is None:
            await self.app(scope, receive, send)
            return

        extra: list[tuple[bytes, bytes]] = []
        if manifest.app:
            extra.append((b"x-deploy-app", manifest.app.encode()))
        if manifest.app_source.sha:
            extra.append((b"x-deploy-sha", manifest.app_source.sha.encode()))
        if manifest.deployed_at:
            extra.append((b"x-deploy-time", manifest.deployed_at.encode()))

        if not extra:
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(extra)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


_HEAD_CLOSE_RE = re.compile(rb"</head\s*>", re.IGNORECASE)
_HEAD_OPEN_RE = re.compile(rb"<head[^>]*>", re.IGNORECASE)


def _meta_snippet(manifest: DeployManifest) -> Optional[bytes]:
    """Build the ``<meta>`` tags for a manifest, or ``None`` if nothing to add.

    Values are HTML-attribute-escaped defensively even though SHAs and ISO
    timestamps are already safe — a manifest is external input (written by a
    deploy tool) and we inject it straight into served HTML.
    """
    tags: list[str] = []
    if manifest.app_source.sha:
        tags.append(_meta_tag("x-deploy-sha", manifest.app_source.sha))
    if manifest.deployed_at:
        tags.append(_meta_tag("x-deploy-time", manifest.deployed_at))
    if not tags:
        return None
    return "".join(tags).encode("utf-8")


def _meta_tag(name: str, content: str) -> str:
    return f'<meta name="{name}" content="{html.escape(content, quote=True)}">'


def _inject_meta(body: bytes, snippet: bytes) -> bytes:
    """Insert ``snippet`` into ``body`` just before ``</head>``.

    Falls back to just after an opening ``<head ...>`` tag, then to
    prepending — so even malformed HTML still carries the tags.
    """
    m = _HEAD_CLOSE_RE.search(body)
    if m:
        return body[: m.start()] + snippet + body[m.start() :]
    m = _HEAD_OPEN_RE.search(body)
    if m:
        return body[: m.end()] + snippet + body[m.end() :]
    return snippet + body


class DeployMetaTagMiddleware(_PrefixManifestMiddleware):
    """Pure-ASGI middleware that injects deploy ``<meta>`` tags into HTML.

    For ``text/html`` responses on app-mounted paths, inserts
    ``<meta name="x-deploy-sha" ...>`` and ``<meta name="x-deploy-time" ...>``
    into ``<head>``. This is what unlocks the browser-cache diagnostic: a
    page can compare its embedded SHA against ``/_meta`` to tell "deploy
    didn't take" from "browser is serving a stale cache".

    Only HTML is touched — JSON, JS, and other assets stream through
    untouched (and carry the ``X-Deploy-*`` headers instead). Because it
    rewrites the body it must run **inside** any compression middleware
    (e.g. GZip), so it sees and edits uncompressed bytes.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        manifest = self._select(scope.get("path", ""))
        snippet = _meta_snippet(manifest) if manifest is not None else None
        if snippet is None:
            await self.app(scope, receive, send)
            return

        # State shared between the start and body handlers. We delay the
        # response.start until the full body is buffered, because injecting
        # changes Content-Length.
        start_message: Optional[dict] = None
        chunks: list[bytes] = []
        intercepting = False

        async def send_wrapper(message):
            nonlocal start_message, intercepting
            mtype = message["type"]

            if mtype == "http.response.start":
                content_type = _header_value(
                    message.get("headers", []), b"content-type"
                )
                if content_type and content_type.lower().startswith(b"text/html"):
                    intercepting = True
                    start_message = message
                    return  # held until body is complete
                await send(message)
                return

            if mtype == "http.response.body" and intercepting:
                chunks.append(message.get("body", b""))
                if message.get("more_body", False):
                    return  # keep buffering until the last chunk
                new_body = _inject_meta(b"".join(chunks), snippet)
                assert start_message is not None
                headers = [
                    (k, v)
                    for (k, v) in start_message.get("headers", [])
                    if k.lower() != b"content-length"
                ]
                headers.append((b"content-length", str(len(new_body)).encode()))
                await send({**start_message, "headers": headers})
                await send(
                    {
                        "type": "http.response.body",
                        "body": new_body,
                        "more_body": False,
                    }
                )
                return

            await send(message)

        await self.app(scope, receive, send_wrapper)


def _header_value(headers, name: bytes) -> Optional[bytes]:
    """Return the first matching header value (case-insensitive), or None."""
    lname = name.lower()
    for k, v in headers:
        if k.lower() == lname:
            return v
    return None
