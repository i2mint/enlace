"""Tests for enlace.app_icons: one icon per app, in every form a browser asks for.

Covers the source tiers (overlay > app_meta > icons_dir > app's own), the PNG
renditions and manifest routes (incl. their access gating), the <head> wiring
middleware (fill gaps vs. replace), and the harvest fixes for mount-absolute
and cache-busted icon hrefs.
"""

import io

import pytest
from PIL import Image
from starlette.testclient import TestClient

from enlace import app_icons
from enlace.appmeta import AppMetaConfig, AppMetaEntry
from enlace.base import PlatformConfig
from enlace.compose import build_backend
from enlace.discover import ConventionDiscoverer


def _png(size=(64, 64), color=(200, 30, 30, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, color).save(out, format="PNG")
    return out.getvalue()


def _app(tmp_path, name="demo", *, head="", access="public", icon=""):
    """A discovered frontend-only app whose index.html has ``head`` in <head>."""
    d = tmp_path / "apps" / name
    fe = d / "frontend"
    fe.mkdir(parents=True)
    (fe / "index.html").write_text(
        f"<!doctype html><html><head><title>{name}</title>{head}</head>"
        "<body>hi</body></html>",
        encoding="utf-8",
    )
    cfg = ConventionDiscoverer().discover_app_dir(d)
    cfg.access = access
    if icon:
        cfg.icon = icon
    return cfg


def _client(apps, **meta):
    return TestClient(
        build_backend(PlatformConfig(apps=apps, app_meta=AppMetaConfig(**meta)))
    )


@pytest.fixture
def icons_dir(tmp_path):
    d = tmp_path / "app_icons"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Source tiers
# ---------------------------------------------------------------------------


def test_icons_dir_file_beats_app_own_icon(tmp_path, icons_dir):
    app = _app(tmp_path, icon="emoji:🎸")
    (icons_dir / "demo.png").write_bytes(_png())
    spec, root, owned = app_icons.icon_source(
        app, PlatformConfig(apps=[app], app_meta=AppMetaConfig(icons_dir=icons_dir)), {}
    )
    assert (spec, root, owned) == ("demo.png", icons_dir, True)


def test_platform_toml_icon_beats_icons_dir(tmp_path, icons_dir):
    app = _app(tmp_path)
    (icons_dir / "demo.png").write_bytes(_png())
    meta = AppMetaConfig(
        icons_dir=icons_dir, apps={"demo": AppMetaEntry(icon="emoji:x")}
    )
    spec, _, owned = app_icons.icon_source(
        app, PlatformConfig(apps=[app], app_meta=meta), {}
    )
    assert spec == "emoji:x" and owned


def test_app_own_icon_is_not_platform_owned(tmp_path):
    app = _app(tmp_path, icon="emoji:🎸")
    spec, _, owned = app_icons.icon_source(app, PlatformConfig(apps=[app]), {})
    assert spec == "emoji:🎸" and not owned


def test_png_preferred_over_svg_in_icons_dir(icons_dir):
    (icons_dir / "demo.svg").write_text("<svg/>")
    (icons_dir / "demo.png").write_bytes(_png())
    assert app_icons.icons_dir_file(icons_dir, "demo") == "demo.png"


# ---------------------------------------------------------------------------
# Renditions + manifest routes
# ---------------------------------------------------------------------------


def test_png_rendition_is_square_and_sized():
    out = app_icons.png_rendition(_png((300, 200)), 180)
    with Image.open(io.BytesIO(out)) as im:
        assert im.size == (180, 180)
        assert im.mode == "RGB"  # flattened: iOS shows transparency as black


def test_png_route_serves_every_size_from_icons_dir(tmp_path, icons_dir):
    app = _app(tmp_path)
    (icons_dir / "demo.png").write_bytes(_png((512, 512)))
    client = _client([app], icons_dir=icons_dir)
    for size in app_icons.PNG_SIZES:
        r = client.get(f"/_apps/demo/icon-{size}.png")
        assert r.status_code == 200, size
        assert r.headers["content-type"] == "image/png"
        with Image.open(io.BytesIO(r.content)) as im:
            assert im.size == (size, size)
    assert client.get("/_apps/demo/icon-77.png").status_code == 404


def test_png_route_404s_for_vector_only_icon(tmp_path):
    app = _app(tmp_path, icon="emoji:🎸")
    assert _client([app]).get("/_apps/demo/icon-180.png").status_code == 404


def test_manifest_scoped_to_app_with_png_icons(tmp_path, icons_dir):
    app = _app(tmp_path)
    (icons_dir / "demo.png").write_bytes(_png())
    r = _client([app], icons_dir=icons_dir).get("/_apps/demo/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["start_url"] == "/demo/" and m["scope"] == "/demo/"
    assert m["display"] == "browser"
    assert [i["sizes"] for i in m["icons"]] == ["192x192", "512x512"]


def test_manifest_and_png_hidden_for_unauthorized_protected_app(tmp_path, icons_dir):
    app = _app(tmp_path, access="protected:user")
    (icons_dir / "demo.png").write_bytes(_png())
    client = _client([app], icons_dir=icons_dir)
    assert client.get("/_apps/demo/manifest.webmanifest").status_code == 404
    assert client.get("/_apps/demo/icon-180.png").status_code == 404


def test_home_screen_gaps_lists_vector_only_apps(tmp_path, icons_dir):
    a = _app(tmp_path, "vec", icon="emoji:🎸")
    b = _app(tmp_path, "ras")
    (icons_dir / "ras.png").write_bytes(_png())
    cfg = PlatformConfig(apps=[a, b], app_meta=AppMetaConfig(icons_dir=icons_dir))
    assert app_icons.home_screen_gaps(cfg) == ["vec"]


# ---------------------------------------------------------------------------
# <head> wiring
# ---------------------------------------------------------------------------


def test_bare_page_gets_all_links(tmp_path, icons_dir):
    app = _app(tmp_path)
    (icons_dir / "demo.png").write_bytes(_png())
    html = _client([app], icons_dir=icons_dir).get("/demo/").text
    head = html.split("</head>")[0]
    assert 'rel="icon"' in head and "/_apps/demo/icon-32.png?v=" in head
    assert 'rel="apple-touch-icon"' in head and "/_apps/demo/icon-180.png?v=" in head
    assert 'rel="manifest" href="/_apps/demo/manifest.webmanifest"' in head
    assert 'name="apple-mobile-web-app-title" content="' in head
    assert "crossorigin" not in head  # public app: no credentials needed


def test_page_own_links_kept_when_icon_is_the_apps_own(tmp_path):
    own = '<link rel="icon" href="mine.svg"><link rel="manifest" href="m.json">'
    app = _app(tmp_path, head=own)
    (app.frontend_dir / "mine.svg").write_text("<svg/>")
    head = _client([app]).get("/demo/").text.split("</head>")[0]
    assert 'href="mine.svg"' in head
    assert head.count('rel="icon"') == 1
    assert "manifest.webmanifest" not in head  # the app ships its own


def test_platform_icon_replaces_page_own_icon_links(tmp_path, icons_dir):
    own = '<link rel="icon" href="old.svg"><link rel="apple-touch-icon" href="old.png">'
    app = _app(tmp_path, head=own)
    (icons_dir / "demo.png").write_bytes(_png())
    head = _client([app], icons_dir=icons_dir).get("/demo/").text.split("</head>")[0]
    assert "old.svg" not in head and "old.png" not in head
    assert "/_apps/demo/icon-180.png" in head


def test_protected_app_manifest_link_sends_credentials(tmp_path):
    app = _app(tmp_path, access="protected:shared")
    head = _client([app]).get("/demo/").text.split("</head>")[0]
    assert 'crossorigin="use-credentials"' in head


def test_non_html_untouched(tmp_path):
    app = _app(tmp_path)
    (app.frontend_dir / "data.json").write_text('{"a": 1}')
    assert _client([app]).get("/demo/data.json").json() == {"a": 1}


# ---------------------------------------------------------------------------
# Harvest: hrefs a browser resolves but the harvester used to miss
# ---------------------------------------------------------------------------


def test_harvest_mount_absolute_and_query_hrefs(tmp_path):
    head = '<link rel="icon" href="/demo/favicon.svg?v=3">'
    app = _app(tmp_path, head=head)
    (app.frontend_dir / "favicon.svg").write_text("<svg/>")
    app = ConventionDiscoverer().discover_app_dir(tmp_path / "apps" / "demo")
    assert app.icon == "frontend/favicon.svg"


def test_harvest_prefers_largest_declared_icon(tmp_path):
    head = (
        '<link rel="icon" sizes="32x32" href="f32.png">'
        '<link rel="apple-touch-icon" href="apple.png">'
    )
    app = _app(tmp_path, head=head)
    (app.frontend_dir / "f32.png").write_bytes(_png((32, 32)))
    (app.frontend_dir / "apple.png").write_bytes(_png((180, 180)))
    app = ConventionDiscoverer().discover_app_dir(tmp_path / "apps" / "demo")
    assert app.icon == "frontend/apple.png"
