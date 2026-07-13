"""Tests for enlace.appmeta and the /_apps launcher metadata + icon surface.

Covers: harvest precedence (incl. the provenance title gate), keyword
union/casefold, icon resolution and content-hash cache-busting, path
containment + content-type allowlist, uniform-404 access gating on the icon
endpoint, the editor-DI contract, and a discovery regression for external /
process apps (which have no frontend on disk).
"""

import base64
import json

import pytest
from starlette.testclient import TestClient

from enlace import appmeta
from enlace.appmeta import AppMetaConfig, AppMetaEntry
from enlace.base import AppConfig, PlatformConfig
from enlace.compose import build_backend
from enlace.discover import ConventionDiscoverer

# ---------------------------------------------------------------------------
# Fixtures: synthetic app dirs (hermetic — no dependency on the real apps)
# ---------------------------------------------------------------------------


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


@pytest.fixture
def app_dir(tmp_path):
    """A frontend-only app dir with an index.html; caller adds files as needed."""
    d = tmp_path / "myapp"
    _write(d / "frontend" / "index.html", "<!doctype html><title>myapp</title>")
    return d


def _discover(app_dir):
    return ConventionDiscoverer().discover_app_dir(app_dir)


# ---------------------------------------------------------------------------
# Keyword union / casefold
# ---------------------------------------------------------------------------


def test_keyword_union_casefold_dedupes_keeps_first_form():
    merged, sources = appmeta.resolve_keywords(
        app_keywords=["Music", "chords"],
        platform_keywords=["music", "Guitar"],
        overlay_keywords=["suno", "SUNO", " chords "],
    )
    # "music"/"Music" dedupe to first surface form; order app -> platform -> overlay
    assert merged == ["Music", "chords", "Guitar", "suno"]
    # sources PARTITION merged: each keyword in exactly one (highest) bucket.
    # "music" is deduped against app's "Music"; overlay's "chords" is deduped
    # against app's "chords" (so it stays read-only, not an editable overlay chip).
    assert sources == {
        "app": ["Music", "chords"],
        "platform": ["Guitar"],
        "overlay": ["suno"],
    }


def test_norm_keywords_drops_blanks_and_nonstrings():
    assert appmeta._norm_keywords(["  a ", "", None, 3, "A", "b"]) == ["a", "b"]


# ---------------------------------------------------------------------------
# Harvest precedence + provenance title gate
# ---------------------------------------------------------------------------


def test_app_toml_title_wins_over_head(app_dir):
    """A title declared in app.toml is Tier C.1 and is NOT overridden by <head>."""
    _write(app_dir / "app.toml", 'display_name = "Real Title"\naccess = "public"\n')
    _write(
        app_dir / "frontend" / "index.html",
        "<!doctype html><head><title>Head Title</title></head>",
    )
    cfg = _discover(app_dir)
    assert cfg.display_name == "Real Title"
    assert cfg.provenance["display_name"] == "override: app.toml"


def test_head_title_used_when_app_toml_absent(app_dir):
    """Without an app.toml title, a clean <head> source supplies the title."""
    _write(
        app_dir / "frontend" / "index.html",
        '<!doctype html><head><meta name="application-name" content="Clean Name">'
        "<title>Clean Name — Suffix</title></head>",
    )
    cfg = _discover(app_dir)
    # application-name is preferred over <title> (which carries a suffix)
    assert cfg.display_name == "Clean Name"
    assert cfg.provenance["display_name"] == "harvest: html-head"


def test_derived_title_when_no_source(tmp_path):
    """No app.toml and no harvestable title ⇒ the auto-derived name (Tier D)."""
    d = tmp_path / "cool_thing"
    _write(d / "frontend" / "index.html", "<!doctype html>")  # no <title>
    cfg = _discover(d)
    assert cfg.display_name == "Cool Thing"
    # Derived default is NOT app.toml provenance
    assert cfg.provenance.get("display_name") != "override: app.toml"


def test_manifest_beats_head_for_description(app_dir):
    _write(
        app_dir / "frontend" / "manifest.webmanifest",
        json.dumps(
            {"name": "M", "description": "From manifest", "categories": ["a", "b"]}
        ),
    )
    _write(
        app_dir / "frontend" / "index.html",
        '<!doctype html><head><meta name="description" content="From head"></head>',
    )
    cfg = _discover(app_dir)
    assert cfg.description == "From manifest"
    assert cfg.keywords == ["a", "b"]


def test_keywords_union_across_app_toml_and_manifest(app_dir):
    _write(
        app_dir / "app.toml",
        'access = "public"\nkeywords = ["hand", "typed"]\n',
    )
    _write(
        app_dir / "frontend" / "manifest.webmanifest",
        json.dumps({"name": "M", "categories": ["typed", "extra"]}),
    )
    cfg = _discover(app_dir)
    # union, casefold-dedup, stable order (app.toml first)
    assert cfg.keywords == ["hand", "typed", "extra"]


# ---------------------------------------------------------------------------
# Icon resolution: monogram / emoji / glyph / data / redirect
# ---------------------------------------------------------------------------


def test_monogram_default_is_svg_with_initials():
    r = appmeta.resolve_icon(
        "", app_name="chord_renderer", display_name="Chord Renderer", app_dir=None
    )
    assert r.content_type == "image/svg+xml"
    assert b"CR" in r.body
    assert r.immutable is True


def test_monogram_hue_is_deterministic():
    assert appmeta._hue_for("chord_renderer") == appmeta._hue_for("chord_renderer")
    # different names (very likely) differ
    assert appmeta._hue_for("a") != appmeta._hue_for("zzzz")


def test_emoji_prefix_renders_glyph():
    r = appmeta.resolve_icon("emoji:🎸", app_name="x", display_name="X", app_dir=None)
    assert r.content_type == "image/svg+xml"
    assert "🎸" in r.body.decode("utf-8")


def test_bare_glyph_renders_without_app_dir():
    r = appmeta.resolve_icon(
        "λ", app_name="lambdalab", display_name="lambdalab", app_dir=None
    )
    assert "λ" in r.body.decode("utf-8")


def test_data_uri_png_served():
    payload = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    r = appmeta.resolve_icon(
        f"data:image/png;base64,{payload}", app_name="x", display_name="X", app_dir=None
    )
    assert r.content_type == "image/png"
    assert r.body == b"\x89PNG\r\n\x1a\n"


def test_data_uri_disallowed_type_falls_back_to_monogram():
    r = appmeta.resolve_icon(
        "data:text/html,<script>evil()</script>",
        app_name="x",
        display_name="X",
        app_dir=None,
    )
    assert r.content_type == "image/svg+xml"  # monogram, not the html


def test_https_url_redirects_not_immutable():
    r = appmeta.resolve_icon(
        "https://cdn.example/logo.png", app_name="x", display_name="X", app_dir=None
    )
    assert r.redirect_url == "https://cdn.example/logo.png"
    assert r.immutable is False
    assert r.body is None


def test_http_url_rejected_falls_back_to_monogram():
    r = appmeta.resolve_icon(
        "http://insecure/logo.png", app_name="x", display_name="X", app_dir=None
    )
    assert r.redirect_url is None
    assert r.content_type == "image/svg+xml"


# ---------------------------------------------------------------------------
# Icon path containment + content-type allowlist + content-hash busting
# ---------------------------------------------------------------------------


def test_contained_file_served_with_content_type(tmp_path):
    _write(tmp_path / "assets" / "favicon-96.png", b"PNGDATA")
    r = appmeta.resolve_icon(
        "assets/favicon-96.png", app_name="x", display_name="X", app_dir=tmp_path
    )
    assert r.content_type == "image/png"
    assert r.body == b"PNGDATA"


def test_traversal_and_absolute_paths_blocked(tmp_path):
    _write(tmp_path / "secret.txt", b"hunter2")
    outside = tmp_path.parent / "outside.png"
    _write(outside, b"nope")
    for spec in ("../outside.png", "../../etc/passwd", str(outside)):
        r = appmeta.resolve_icon(spec, app_name="x", display_name="X", app_dir=tmp_path)
        # never serves the file; always the monogram
        assert r.content_type == "image/svg+xml", spec
        assert r.body is not None and b"hunter2" not in r.body


def test_symlink_escape_blocked(tmp_path):
    secret = tmp_path.parent / "secret.png"
    _write(secret, b"leak")
    link = tmp_path / "link.png"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unsupported here")
    r = appmeta.resolve_icon(
        "link.png", app_name="x", display_name="X", app_dir=tmp_path
    )
    assert (
        r.content_type == "image/svg+xml"
    )  # resolve() collapses the symlink → outside → monogram


def test_disallowed_extension_falls_back(tmp_path):
    _write(tmp_path / "thing.exe", b"MZ")
    r = appmeta.resolve_icon(
        "thing.exe", app_name="x", display_name="X", app_dir=tmp_path
    )
    assert r.content_type == "image/svg+xml"


def test_content_hash_busts_on_file_change(tmp_path):
    p = tmp_path / "logo.png"
    _write(p, b"v1")
    t1 = appmeta.resolve_icon(
        "logo.png", app_name="x", display_name="X", app_dir=tmp_path
    ).token
    _write(p, b"v2-different-bytes")
    t2 = appmeta.resolve_icon(
        "logo.png", app_name="x", display_name="X", app_dir=tmp_path
    ).token
    assert t1 != t2


# ---------------------------------------------------------------------------
# Discovery regression: external / process apps have no frontend on disk
# ---------------------------------------------------------------------------


def test_external_and_process_apps_resolve_without_crashing(tmp_path):
    """A mode=external app (typola-like) and mode=process app (trufflepig-like)
    have no frontend dir; harvest must no-op, not raise, and they get monograms."""
    ext = tmp_path / "typola"
    _write(
        ext / "app.toml",
        'mode = "external"\nupstream_url = "https://x.hf.space"\naccess = "public"\n',
    )
    proc = tmp_path / "connector"
    _write(
        proc / "app.toml",
        'mode = "process"\ncommand = "uvicorn x:app"\nport = 8030\naccess = "public"\n',
    )
    disc = ConventionDiscoverer()
    for d in (ext, proc):
        cfg = disc.discover_app_dir(d)
        assert cfg is not None
        assert cfg.icon == ""  # nothing harvested; monogram at serve time
        r = appmeta.resolve_icon(
            cfg.icon,
            app_name=cfg.name,
            display_name=cfg.display_name,
            app_dir=appmeta.app_dir_of(cfg),
        )
        assert r.content_type == "image/svg+xml"


# ---------------------------------------------------------------------------
# /_apps payload + icon endpoint (integration)
# ---------------------------------------------------------------------------


def _make_public_app(name="pubapp", *, icon="", keywords=None):
    return AppConfig(
        name=name,
        route_prefix=f"/api/{name}",
        app_type="frontend_only",
        access="public",
        icon=icon,
        keywords=keywords or [],
    )


def test_apps_payload_has_new_fields_and_launchability(tmp_path):
    app = _make_public_app()
    cfg = PlatformConfig(apps=[app])
    client = TestClient(build_backend(cfg))
    data = client.get("/_apps").json()
    assert data["can_edit_meta"] is False
    item = data["apps"][0]
    for key in (
        "keyword_sources",
        "icon_url",
        "launchable",
        "launch_url",
        "description",
    ):
        assert key in item
    # frontend_only with no real dir ⇒ not launchable
    assert item["launchable"] is False


def test_external_app_is_launchable_at_route_prefix():
    app = AppConfig(
        name="typola",
        route_prefix="/typola",
        app_type="asgi_app",
        mode="external",
        upstream_url="https://x",
        access="public",
    )
    cfg = PlatformConfig(apps=[app])
    item = TestClient(build_backend(cfg)).get("/_apps").json()["apps"][0]
    assert item["launchable"] is True
    assert item["launch_url"] == "/typola/"


def test_icon_endpoint_serves_image_not_shadowed_by_landing(tmp_path):
    """GET /_apps/{name}/icon returns image/*, and isn't swallowed by any / mount."""
    app = _make_public_app(icon="emoji:🎸")
    cfg = PlatformConfig(apps=[app])
    client = TestClient(build_backend(cfg))
    r = client.get(f"/_apps/{app.name}/icon")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    # ETag corresponds to the ?v= token in the listing
    token = r.headers["etag"].strip('"')
    listing = client.get("/_apps").json()["apps"][0]
    assert f"v={token}" in listing["icon_url"]


def test_icon_endpoint_not_shadowed_by_root_catchall(tmp_path):
    """With a shared_assets_dir StaticFiles mounted at '/', the icon route still wins.

    The earlier test has no '/' mount, so it can't catch a route-ordering
    regression. This one actually mounts the catch-all the icon route must beat.
    """
    assets = tmp_path / "assets"
    assets.mkdir()
    app = _make_public_app(icon="emoji:🎸")
    cfg = PlatformConfig(apps=[app], shared_assets_dir=assets)
    r = TestClient(build_backend(cfg)).get(f"/_apps/{app.name}/icon")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")


def test_icon_response_has_csp_and_nosniff():
    """SVG icons carry a CSP that neutralises script if fetched as a document."""
    app = _make_public_app(icon="emoji:🎸")
    r = TestClient(build_backend(PlatformConfig(apps=[app]))).get(
        f"/_apps/{app.name}/icon"
    )
    assert r.headers["x-content-type-options"] == "nosniff"
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "sandbox" in csp


def test_file_icon_token_is_stat_based_and_busts(tmp_path):
    """The file-icon ?v= token comes from stat (no byte read at /_apps) and still
    busts when the file changes; token_only and full resolve agree."""
    d = tmp_path / "app_x"
    (d / "assets").mkdir(parents=True)
    icon = d / "assets" / "logo.png"
    icon.write_bytes(b"v1")
    from enlace import appmeta

    t_full = appmeta.resolve_icon(
        "assets/logo.png", app_name="app_x", display_name="X", app_dir=d
    ).token
    t_tokenonly = appmeta.resolve_icon(
        "assets/logo.png",
        app_name="app_x",
        display_name="X",
        app_dir=d,
        token_only=True,
    ).token
    assert t_full == t_tokenonly  # consistent → ETag matches ?v=
    # token_only must NOT read bytes
    assert (
        appmeta.resolve_icon(
            "assets/logo.png",
            app_name="app_x",
            display_name="X",
            app_dir=d,
            token_only=True,
        ).body
        is None
    )
    icon.write_bytes(b"v2-larger-content")  # size changes → token busts
    t2 = appmeta.resolve_icon(
        "assets/logo.png", app_name="app_x", display_name="X", app_dir=d
    ).token
    assert t2 != t_full


def test_harvest_manifest_provenance(tmp_path):
    """A title harvested from a Web App Manifest records 'harvest: manifest'."""
    d = tmp_path / "mani"
    (d / "frontend").mkdir(parents=True)
    (d / "frontend" / "index.html").write_text("<!doctype html>")
    (d / "frontend" / "manifest.webmanifest").write_text(
        '{"name": "Manifest Title", "description": "from manifest"}'
    )
    cfg = ConventionDiscoverer().discover_app_dir(d)
    assert cfg.display_name == "Manifest Title"
    assert cfg.provenance["display_name"] == "harvest: manifest"
    assert cfg.provenance["description"] == "harvest: manifest"


def test_icon_endpoint_uniform_404_for_unknown_and_hidden(tmp_path):
    """Unknown app AND an app the caller can't see both return a bare 404."""
    hidden = AppConfig(
        name="secret",
        route_prefix="/api/secret",
        app_type="frontend_only",
        access="protected:user",
        allowed_users=["someone@else.com"],
    )
    cfg = PlatformConfig(apps=[hidden])
    client = TestClient(build_backend(cfg))
    # anonymous caller: unknown app and hidden app are indistinguishable
    assert client.get("/_apps/nope/icon").status_code == 404
    assert client.get("/_apps/secret/icon").status_code == 404
    # hidden app is not in the listing either
    assert client.get("/_apps").json()["apps"] == []


def test_can_edit_meta_reflects_injected_closure():
    """The DI contract: core reports can_edit_meta from the plugin's closure."""
    app = _make_public_app()
    cfg = PlatformConfig(apps=[app])
    backend = build_backend(cfg)
    backend.state.app_meta_can_edit = lambda email: email == "boss@x.com"
    client = TestClient(backend)
    assert client.get("/_apps").json()["can_edit_meta"] is False  # anonymous


def test_overlay_keywords_and_icon_merged_from_di_store():
    """Core reads an injected overlay mapping (Tier A) for keywords + icon."""
    app = _make_public_app(keywords=["base"])
    cfg = PlatformConfig(
        apps=[app],
        app_meta=AppMetaConfig(apps={"pubapp": AppMetaEntry(keywords=["platform"])}),
    )
    backend = build_backend(cfg)
    backend.state.app_meta_overlay = {
        "pubapp": {"keywords": ["added"], "icon": "emoji:⭐"}
    }
    client = TestClient(backend)
    item = client.get("/_apps").json()["apps"][0]
    assert item["keywords"] == ["base", "platform", "added"]
    assert item["keyword_sources"] == {
        "app": ["base"],
        "platform": ["platform"],
        "overlay": ["added"],
    }
    # overlay icon wins
    r = client.get("/_apps/pubapp/icon")
    assert "⭐" in r.content.decode("utf-8")


# ---------------------------------------------------------------------------
# updated_at: the launcher's "last updated" sort key
# ---------------------------------------------------------------------------


def _write_manifest(manifest_dir, app_name, *, committed_at=None, deployed_at=None):
    """Drop a deploy manifest for `app_name` (what deploy.py writes at deploy time)."""
    manifest_dir.mkdir(parents=True, exist_ok=True)
    body = {"schema_version": 1, "app": app_name, "app_source": {"sha": "abc123"}}
    if committed_at is not None:
        body["app_source"]["committed_at"] = committed_at
    if deployed_at is not None:
        body["deployed_at"] = deployed_at
    _write(manifest_dir / f"{app_name}.json", json.dumps(body))


def _updated_at_of(cfg):
    return TestClient(build_backend(cfg)).get("/_apps").json()["apps"][0]["updated_at"]


def test_updated_at_prefers_committed_at_over_deployed_at(tmp_path):
    """When the source last CHANGED beats when we last SHIPPED it.

    A full deploy stamps every app with the same `deployed_at`, so ordering by it
    would tie the whole grid. `committed_at` is the signal that actually varies.
    """
    manifests = tmp_path / "manifests"
    _write_manifest(
        manifests,
        "pubapp",
        committed_at="2026-04-28T12:16:50+02:00",
        deployed_at="2026-07-13T07:02:33+00:00",
    )
    cfg = PlatformConfig(apps=[_make_public_app()], manifest_dir=manifests)
    assert _updated_at_of(cfg) == "2026-04-28T12:16:50+02:00"


def test_updated_at_falls_back_to_deployed_at(tmp_path):
    """Manifests written before `committed_at` existed still date their app."""
    manifests = tmp_path / "manifests"
    _write_manifest(manifests, "pubapp", deployed_at="2026-07-13T07:02:33+00:00")
    cfg = PlatformConfig(apps=[_make_public_app()], manifest_dir=manifests)
    assert _updated_at_of(cfg) == "2026-07-13T07:02:33+00:00"


def test_updated_at_is_null_without_a_manifest(tmp_path):
    """No manifest ⇒ unknown, NOT epoch-zero.

    The launcher sorts unknowns last rather than pretending they are ancient, so
    an app that has never been deployed with a manifest-writing deploy.py does not
    masquerade as the oldest thing on the platform.
    """
    cfg = PlatformConfig(apps=[_make_public_app()], manifest_dir=tmp_path / "empty")
    assert _updated_at_of(cfg) is None


def test_updated_at_survives_a_metadata_edit(tmp_path):
    """`build_launcher_item` carries updated_at, so an overlay edit can't drop it.

    enlace_auth's PATCH handler returns a freshly-built item; if the date lived
    anywhere but on the AppConfig, an edited app would come back undated and the
    tile would fall to the bottom of the "last updated" sort.
    """
    from enlace.compose import build_launcher_item

    manifests = tmp_path / "manifests"
    _write_manifest(manifests, "pubapp", committed_at="2026-04-28T12:16:50+02:00")
    app = _make_public_app()
    cfg = PlatformConfig(apps=[app], manifest_dir=manifests)
    build_backend(cfg)  # stamps updated_at onto the AppConfig at startup

    item = build_launcher_item(app, cfg, {"description": "edited"})
    assert item["description"] == "edited"
    assert item["updated_at"] == "2026-04-28T12:16:50+02:00"
