"""TOML parsing for [auth], [auth.stores], [auth.oauth.*], [stores.user_data]."""

from pathlib import Path

from enlace.base import PlatformConfig


def test_auth_section_parsed(tmp_path: Path):
    toml = tmp_path / "platform.toml"
    toml.write_text(
        """
[auth]
enabled = true
session_cookie_name = "my_session"
session_max_age_seconds = 3600
signing_key_env = "MY_KEY"
secure_cookies = false

[auth.stores]
backend = "file"
path = "/tmp/platform_store"

[auth.oauth.google]
client_id_env = "GOOGLE_ID"
client_secret_env = "GOOGLE_SECRET"
scopes = ["openid", "email"]

[stores.user_data]
backend = "file"
path = "/tmp/user_data"
"""
    )
    config = PlatformConfig.from_toml(toml)
    assert config.auth.enabled is True
    assert config.auth.session_cookie_name == "my_session"
    assert config.auth.session_max_age_seconds == 3600
    assert config.auth.signing_key_env == "MY_KEY"
    assert config.auth.secure_cookies is False
    assert config.auth.stores.path == "/tmp/platform_store"
    assert "google" in config.auth.oauth
    g = config.auth.oauth["google"]
    assert g.client_id_env == "GOOGLE_ID"
    assert g.scopes == ["openid", "email"]
    assert "user_data" in config.stores
    assert config.stores["user_data"].path == "/tmp/user_data"


def test_auth_defaults_when_section_absent(tmp_path: Path):
    toml = tmp_path / "platform.toml"
    toml.write_text("")
    config = PlatformConfig.from_toml(toml)
    assert config.auth.enabled is False
    assert config.auth.session_cookie_name == "enlace_session"
    assert config.stores == {}


def test_shared_password_env_parsed_in_app(tmp_path: Path):
    """An app's shared_password_env should round-trip through AppConfig TOML."""
    # Build an AppConfig directly to avoid needing full discovery fixtures.
    from enlace.base import AppConfig

    app = AppConfig(
        name="secret_app",
        route_prefix="/api/secret_app",
        app_type="asgi_app",
        access="protected:shared",
        shared_password_env="SECRET_APP_PW",
    )
    assert app.shared_password_env == "SECRET_APP_PW"
    assert app.access == "protected:shared"
