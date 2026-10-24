"""Tests for enlace.app_identity: the per-app head/icon checklist."""

from enlace.app_identity import identity_gaps
from enlace.base import PlatformConfig
from enlace.doctor import WARN, run_doctor
from enlace.tests.test_app_icons import _app, _png

_FULL_HEAD = (
    '<meta name="description" content="d">'
    '<meta name="theme-color" content="#fff">'
    '<meta property="og:image" content="og.png">'
    '<link rel="apple-touch-icon" href="icon.png">'
)


def _full_app(tmp_path, *, px=512):
    app = _app(tmp_path, head=_FULL_HEAD)
    (app.frontend_dir / "icon.png").write_bytes(_png((px, px)))
    return _app_rediscover(tmp_path)


def _app_rediscover(tmp_path):
    from enlace.discover import ConventionDiscoverer

    return ConventionDiscoverer().discover_app_dir(tmp_path / "apps" / "demo")


def test_complete_app_has_no_gaps(tmp_path):
    app = _full_app(tmp_path)
    assert identity_gaps(PlatformConfig(apps=[app])) == {}


def test_bare_app_lists_every_gap(tmp_path):
    app = _app(tmp_path)
    gaps = identity_gaps(PlatformConfig(apps=[app]))["demo"]
    assert {"description", "theme-color", "og-image", "icon-png"} <= set(gaps)


def test_small_png_source_misses_512(tmp_path):
    app = _full_app(tmp_path, px=180)
    assert identity_gaps(PlatformConfig(apps=[app])) == {"demo": ["icon-512"]}


def test_doctor_warns_but_stays_green(tmp_path):
    report = run_doctor(PlatformConfig(apps=[_app(tmp_path)]))
    check = next(c for c in report.checks if c.name == "identity:demo")
    assert check.status == WARN and "og-image" in check.extra["missing"]
    assert report.ok
