"""Fail-fast behavior when [auth].enabled and the signing key is unusable.

Covers the silent-degradation regression from i2mint/enlace#11: if the signing
key env var is missing, the gateway used to boot with /auth/* un-mounted. It
now refuses to start unless the operator sets ENLACE_ALLOW_UNSIGNED=1.
"""

from __future__ import annotations

import os

import pytest

from enlace import EnlaceConfigError, build_backend
from enlace.base import AuthConfig, PlatformConfig
from enlace.discover import discover_apps

_KEY_ENV = "ENLACE_TEST_SIGNING_KEY"
_OPT_OUT = "ENLACE_ALLOW_UNSIGNED"
_GOOD_KEY = "x" * 48  # any string >= 32 chars passes the length check


@pytest.fixture
def auth_config():
    return AuthConfig(enabled=True, signing_key_env=_KEY_ENV, secure_cookies=False)


@pytest.fixture
def clean_env(monkeypatch):
    for var in (_KEY_ENV, _OPT_OUT):
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


def _config_with_auth(single_app_dir, auth_cfg):
    cfg = PlatformConfig(apps_dir=single_app_dir, auth=auth_cfg)
    return discover_apps(cfg)


def test_missing_signing_key_raises(clean_env, single_app_dir, auth_config):
    """No ENLACE_SIGNING_KEY + auth enabled → EnlaceConfigError at build time."""
    cfg = _config_with_auth(single_app_dir, auth_config)
    with pytest.raises(EnlaceConfigError) as exc_info:
        build_backend(cfg)
    msg = str(exc_info.value)
    assert _KEY_ENV in msg
    assert "auth-generate-signing-key" in msg


def test_empty_signing_key_raises(clean_env, single_app_dir, auth_config):
    """Whitespace-only key is treated as empty."""
    clean_env.setenv(_KEY_ENV, "   ")
    cfg = _config_with_auth(single_app_dir, auth_config)
    with pytest.raises(EnlaceConfigError):
        build_backend(cfg)


def test_short_signing_key_raises(clean_env, single_app_dir, auth_config):
    """Keys below the minimum length are rejected as malformed."""
    clean_env.setenv(_KEY_ENV, "too-short")
    cfg = _config_with_auth(single_app_dir, auth_config)
    with pytest.raises(EnlaceConfigError) as exc_info:
        build_backend(cfg)
    assert "too short" in str(exc_info.value)


def test_opt_out_env_keeps_current_behavior(
    clean_env, single_app_dir, auth_config, caplog
):
    """ENLACE_ALLOW_UNSIGNED=1 suppresses the raise and logs a loud error."""
    clean_env.setenv(_OPT_OUT, "1")
    cfg = _config_with_auth(single_app_dir, auth_config)
    with caplog.at_level("ERROR", logger="enlace"):
        app = build_backend(cfg)
    assert app is not None
    # The log line must clearly state that auth was disabled.
    joined = "\n".join(r.message for r in caplog.records)
    assert "auth" in joined.lower()
    assert "disabled" in joined.lower() or "unsigned" in joined.lower()


def test_good_key_builds_normally(clean_env, single_app_dir, auth_config):
    """A key of sufficient length lets the gateway build as before."""
    clean_env.setenv(_KEY_ENV, _GOOD_KEY)
    cfg = _config_with_auth(single_app_dir, auth_config)
    app = build_backend(cfg)
    # /auth/csrf must be routable (proof the router was mounted).
    from starlette.testclient import TestClient

    client = TestClient(app)
    resp = client.get("/auth/csrf")
    assert resp.status_code == 200
    assert "csrf" in resp.json()


def test_auth_disabled_is_unaffected_by_missing_key(clean_env, single_app_dir):
    """When [auth].enabled=False, missing key is fine — no auth wiring at all."""
    cfg = PlatformConfig(
        apps_dir=single_app_dir,
        auth=AuthConfig(enabled=False, signing_key_env=_KEY_ENV),
    )
    cfg = discover_apps(cfg)
    app = build_backend(cfg)
    assert app is not None
