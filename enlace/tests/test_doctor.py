"""Tests for enlace.doctor — static checks + HTTP probes.

HTTP probes are tested against a real ASGI app served by a background
Uvicorn process via starlette's TestClient-less route. We spin up a gateway
in-process via `starlette.testclient.TestClient` for /auth/csrf semantics,
and monkeypatch `doctor._http_get` for the offline cases.
"""

from __future__ import annotations

import pytest

from enlace import doctor as doctor_mod
from enlace.base import AuthConfig, PlatformConfig
from enlace.compose import build_backend
from enlace.discover import discover_apps

_KEY_ENV = "ENLACE_TEST_SIGNING_KEY"
_GOOD_KEY = "x" * 48


@pytest.fixture
def clean_env(monkeypatch):
    for v in (_KEY_ENV, "ENLACE_ALLOW_UNSIGNED"):
        monkeypatch.delenv(v, raising=False)
    yield monkeypatch


def _auth_enabled_config(single_app_dir):
    cfg = PlatformConfig(
        apps_dir=single_app_dir,
        auth=AuthConfig(enabled=True, signing_key_env=_KEY_ENV, secure_cookies=False),
    )
    return discover_apps(cfg)


def test_static_missing_signing_key_is_fail(clean_env, single_app_dir):
    """Static check catches the original incident regardless of HTTP probing."""
    cfg = _auth_enabled_config(single_app_dir)
    report = doctor_mod.run_doctor(cfg, base_url=None)
    sk = [c for c in report.checks if c.name == "signing_key"][0]
    assert sk.status == doctor_mod.FAIL
    assert _KEY_ENV in sk.detail
    assert not report.ok


def test_static_good_signing_key_passes(clean_env, single_app_dir):
    clean_env.setenv(_KEY_ENV, _GOOD_KEY)
    cfg = _auth_enabled_config(single_app_dir)
    report = doctor_mod.run_doctor(cfg, base_url=None)
    sk = [c for c in report.checks if c.name == "signing_key"][0]
    assert sk.status == doctor_mod.PASS
    assert report.ok


def test_static_skip_when_auth_disabled(clean_env, single_app_dir):
    cfg = PlatformConfig(
        apps_dir=single_app_dir, auth=AuthConfig(enabled=False, signing_key_env=_KEY_ENV)
    )
    cfg = discover_apps(cfg)
    report = doctor_mod.run_doctor(cfg, base_url=None)
    sk = [c for c in report.checks if c.name == "signing_key"][0]
    assert sk.status == doctor_mod.SKIP
    assert report.ok


def test_report_format_text_renders(clean_env, single_app_dir):
    cfg = _auth_enabled_config(single_app_dir)
    report = doctor_mod.run_doctor(cfg, base_url=None)
    text = report.format_text()
    assert "enlace doctor" in text
    assert "signing_key" in text
    assert "Result:" in text


def test_report_as_dict_shape(clean_env, single_app_dir):
    clean_env.setenv(_KEY_ENV, _GOOD_KEY)
    cfg = _auth_enabled_config(single_app_dir)
    report = doctor_mod.run_doctor(cfg, base_url=None)
    data = report.as_dict()
    assert "ok" in data and "summary" in data and "checks" in data
    assert isinstance(data["checks"], list)
    assert all({"name", "status", "detail"} <= c.keys() for c in data["checks"])


# ---------------------------------------------------------------------------
# HTTP probe checks — use a patched _http_get rather than a live server to
# keep tests fast and portable.
# ---------------------------------------------------------------------------


def _fake_http(map_: dict):
    """Build a fake _http_get returning canned responses keyed by URL."""

    def _get(url, *, timeout):  # noqa: ARG001
        resp = map_.get(url)
        if resp is None:
            return None, {}, None, f"unexpected URL in test: {url}"
        return resp  # (status, headers, body, error)

    return _get


def test_csrf_probe_catches_spa_fallthrough(clean_env, single_app_dir, monkeypatch):
    """The canonical regression: /auth/csrf returns the SPA's index.html."""
    clean_env.setenv(_KEY_ENV, _GOOD_KEY)
    cfg = _auth_enabled_config(single_app_dir)

    spa_html = b"<!doctype html><html><body>SPA</body></html>"
    monkeypatch.setattr(
        doctor_mod,
        "_http_get",
        _fake_http(
            {
                "http://x/auth/csrf": (200, {"content-type": "text/html"}, spa_html, None),
                "http://x/foo/": (200, {"content-type": "text/html"}, b"<html/>", None),
                "http://x/api/foo/": (200, {"content-type": "application/json"}, b"{}", None),
            }
        ),
    )
    report = doctor_mod.run_doctor(cfg, base_url="http://x")
    csrf = [c for c in report.checks if c.name == "http:/auth/csrf"][0]
    assert csrf.status == doctor_mod.FAIL
    assert "auth silently disabled" in csrf.detail or "expected JSON" in csrf.detail
    assert not report.ok


def test_csrf_probe_accepts_valid_json(clean_env, single_app_dir, monkeypatch):
    clean_env.setenv(_KEY_ENV, _GOOD_KEY)
    cfg = _auth_enabled_config(single_app_dir)

    monkeypatch.setattr(
        doctor_mod,
        "_http_get",
        _fake_http(
            {
                "http://x/auth/csrf": (
                    200,
                    {"content-type": "application/json"},
                    b'{"csrf": "abc123"}',
                    None,
                ),
                "http://x/api/foo/": (200, {"content-type": "application/json"}, b"{}", None),
            }
        ),
    )
    report = doctor_mod.run_doctor(cfg, base_url="http://x")
    csrf = [c for c in report.checks if c.name == "http:/auth/csrf"][0]
    assert csrf.status == doctor_mod.PASS
    assert report.ok


def test_api_probe_fails_on_5xx(clean_env, single_app_dir, monkeypatch):
    clean_env.setenv(_KEY_ENV, _GOOD_KEY)
    cfg = _auth_enabled_config(single_app_dir)

    monkeypatch.setattr(
        doctor_mod,
        "_http_get",
        _fake_http(
            {
                "http://x/auth/csrf": (
                    200,
                    {"content-type": "application/json"},
                    b'{"csrf": "ok"}',
                    None,
                ),
                "http://x/api/foo/": (503, {}, b"", None),
            }
        ),
    )
    report = doctor_mod.run_doctor(cfg, base_url="http://x")
    assert not report.ok
    api = [c for c in report.checks if c.name == "http:/api/foo/"][0]
    assert api.status == doctor_mod.FAIL


def test_live_gateway_roundtrip(clean_env, single_app_dir):
    """Smoke: with a good key, /auth/csrf on a real TestClient returns JSON.

    We don't use the doctor's urllib path here (TestClient doesn't serve HTTP);
    we just re-verify that build_backend mounts the auth router when the key
    is valid — the condition doctor's HTTP probe depends on.
    """
    clean_env.setenv(_KEY_ENV, _GOOD_KEY)
    cfg = _auth_enabled_config(single_app_dir)
    app = build_backend(cfg)
    from starlette.testclient import TestClient

    client = TestClient(app)
    resp = client.get("/auth/csrf")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert "csrf" in resp.json()
