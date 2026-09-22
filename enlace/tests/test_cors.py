"""CORS never lets another origin read a signed-in user's responses by default.

``allow_origins=["*"]`` together with ``allow_credentials=True`` makes
Starlette reflect *any* request ``Origin`` and add
``Access-Control-Allow-Credentials: true`` -- so any page that got the
browser to attach the session cookie could read the response. The default is
now "any origin, anonymously"; credentials need an explicit origin list.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from enlace.base import PlatformConfig
from enlace.compose import build_backend
from enlace.discover import discover_apps


@pytest.fixture
def apps_dir(tmp_path):
    d = tmp_path / "apps" / "p"
    d.mkdir(parents=True)
    (d / "server.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n"
        "@app.get('/ping')\ndef ping():\n    return {'ok': True}\n"
    )
    return tmp_path / "apps"


def _client(apps_dir, **kw):
    cfg = discover_apps(PlatformConfig(apps_dir=apps_dir, **kw))
    return TestClient(build_backend(cfg))


def _get(client, origin):
    return client.get(
        "/api/p/ping", headers={"Origin": origin, "Cookie": "enlace_session=x"}
    )


def test_default_allows_any_origin_but_never_credentials(apps_dir):
    r = _get(_client(apps_dir), "https://evil.example")
    assert r.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in r.headers


def test_default_preflight_does_not_grant_credentials(apps_dir):
    r = _client(apps_dir).options(
        "/api/p/ping",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-credentials" not in r.headers


def test_explicit_origins_get_credentials_others_nothing(apps_dir):
    c = _client(apps_dir, cors_origins=["https://app.example.com"])
    ok = _get(c, "https://app.example.com")
    assert ok.headers["access-control-allow-origin"] == "https://app.example.com"
    assert ok.headers["access-control-allow-credentials"] == "true"
    other = _get(c, "https://evil.example")
    assert "access-control-allow-origin" not in other.headers


def test_empty_list_disables_cors(apps_dir):
    r = _get(_client(apps_dir, cors_origins=[]), "https://evil.example")
    assert "access-control-allow-origin" not in r.headers


def test_platform_toml_sets_it(tmp_path, apps_dir):
    toml = tmp_path / "platform.toml"
    toml.write_text(
        f'[platform]\napps_dir = "{apps_dir}"\ncors_origins = ["https://a.example"]\n'
    )
    assert PlatformConfig.from_toml(toml).cors_origins == ["https://a.example"]


@pytest.mark.parametrize(
    "origins",
    [
        ["*", "https://a.example"],
        ["null"],
        ["https://a.example/"],
        ["https://a.example/app"],
        ["a.example"],
        ["ftp://a.example"],
        ["https://u@a.example"],
    ],
)
def test_misleading_origin_lists_are_refused(apps_dir, origins):
    with pytest.raises(ValueError):
        PlatformConfig(apps_dir=apps_dir, cors_origins=origins)


def test_valid_origin_list_with_port(apps_dir):
    cfg = PlatformConfig(
        apps_dir=apps_dir, cors_origins=["https://a.example", "http://localhost:5173"]
    )
    assert cfg.cors_origins == ["https://a.example", "http://localhost:5173"]
