"""Every URL enlace emits for an app is percent-encoded (#75).

App names are directory names, so ``my app`` or ``100%`` are possible. Mounting
works on the decoded path; the ``Location`` of the trailing-slash redirect, the
launcher's links, the injected icon/manifest links and the index page's links
must still be valid URLs.
"""

import json
import re

import pytest
from starlette.testclient import TestClient

from enlace.appmeta import AppMetaConfig
from enlace.base import PlatformConfig
from enlace.compose import build_backend
from enlace.discover import ConventionDiscoverer

NAMES = {"my app": "my%20app", "100%": "100%25", "café": "caf%C3%A9"}


def _app(tmp_path, name):
    fe = tmp_path / "apps" / name / "frontend"
    fe.mkdir(parents=True)
    (fe / "index.html").write_text(
        "<!doctype html><html><head><title>t</title></head><body>hi</body></html>",
        encoding="utf-8",
    )
    cfg = ConventionDiscoverer().discover_app_dir(tmp_path / "apps" / name)
    cfg.access = "public"
    return cfg


@pytest.fixture
def client(tmp_path):
    apps = [_app(tmp_path, n) for n in NAMES]
    return TestClient(
        build_backend(PlatformConfig(apps=apps, app_meta=AppMetaConfig()))
    )


@pytest.mark.parametrize("name, encoded", NAMES.items())
def test_trailing_slash_redirect_location_is_encoded(client, name, encoded):
    r = client.get(f"/{encoded}", follow_redirects=False)
    assert r.status_code in (301, 302, 307, 308), r.status_code
    assert r.headers["location"].endswith(f"/{encoded}/")
    assert client.get(r.headers["location"]).status_code == 200


@pytest.mark.parametrize("name, encoded", NAMES.items())
def test_launcher_links_are_encoded(client, name, encoded):
    items = {i["name"]: i for i in client.get("/_apps").json()["apps"]}
    item = items[name]
    assert item["route"] == f"/{encoded}/"
    assert item["launch_url"] == f"/{encoded}/"
    assert item["icon_url"].startswith(f"/_apps/{encoded}/icon?v=")
    assert client.get(item["icon_url"]).status_code == 200


@pytest.mark.parametrize("name, encoded", NAMES.items())
def test_injected_head_links_and_manifest_are_encoded(client, name, encoded):
    page = client.get(f"/{encoded}/").text
    hrefs = re.findall(r'href="([^"]*_apps[^"]*)"', page)
    assert hrefs and all(h.startswith(f"/_apps/{encoded}/") for h in hrefs), hrefs
    manifest = [h for h in hrefs if "manifest" in h]
    if manifest:
        m = json.loads(client.get(manifest[0]).text)
        assert m["start_url"] == m["scope"] == f"/{encoded}/"
        assert all(i["src"].startswith(f"/_apps/{encoded}/") for i in m["icons"])
