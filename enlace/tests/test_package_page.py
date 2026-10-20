"""Tests for enlace.package_page — an app's page served from its Python package."""

import sys
import textwrap

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from enlace.base import AppConfig, PlatformConfig
from enlace.compose import build_backend
from enlace.discover import discover_apps
from enlace.package_page import PackagePageConfig, page_shell

PKG = "enlace_test_pagepkg"

APP_CODE = textwrap.dedent("""\
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse

    app = FastAPI()

    @app.get("/hello")
    def hello():
        return {"message": "hello"}

    @app.get("/ui/{name:path}")  # the app's own route, for running alone
    def ui(name: str):
        return PlainTextResponse("served by the app")
""")

SHELL_MODULE = textwrap.dedent("""\
    def page_shell(*, api, base="", title="pkg"):
        return f"<!doctype html><p>own shell api={api} base={base} title={title}</p>"

    def not_html(*, api, base="", title=""):
        return 42
""")


@pytest.fixture
def page_pkg(tmp_path, monkeypatch):
    """An importable package shipping a page under data/ui, plus a shell module."""
    root = tmp_path / "site"
    pkg = root / PKG
    ui = pkg / "data" / "ui"
    (ui / "vendor").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "web.py").write_text(SHELL_MODULE)
    (ui / "app.js").write_text("console.log('from the package');")
    (ui / "style.css").write_text("body{}")
    (ui / "vendor" / "lib.js").write_text("// vendored")
    (ui / "secret.txt").write_text("not on the allowlist")
    monkeypatch.syspath_prepend(str(root))
    for name in [m for m in sys.modules if m.split(".")[0] == PKG]:
        monkeypatch.delitem(sys.modules, name)
    yield pkg
    for name in [m for m in sys.modules if m.split(".")[0] == PKG]:
        sys.modules.pop(name, None)


def _platform(tmp_path, app_toml: str, *, code: str = APP_CODE, **platform_kw):
    app_dir = tmp_path / "apps" / "demo"
    app_dir.mkdir(parents=True)
    if code:
        (app_dir / "server.py").write_text(code)
    (app_dir / "app.toml").write_text(textwrap.dedent(app_toml))
    return discover_apps(
        PlatformConfig(apps_dir=tmp_path / "apps", **platform_kw),
        on_import_error="record",
    )


def _client(tmp_path, app_toml: str, **kw) -> TestClient:
    return TestClient(build_backend(_platform(tmp_path, app_toml, **kw)))


# -- config -------------------------------------------------------------------


def test_a_string_is_the_package_name():
    config = PackagePageConfig.model_validate("songleaf")
    assert (config.package, config.assets, config.assets_route) == (
        "songleaf",
        "data/ui",
        "ui",
    )
    assert config.global_name == "SONGLEAF"
    assert (config.scripts, config.stylesheets) == (["app.js"], ["style.css"])


@pytest.mark.parametrize(
    "fields",
    [
        {"package": "p", "assets": "../outside"},
        {"package": "p", "assets": ""},
        {"package": "p", "shell": "no_colon"},
        {"package": "p", "global_name": "not-an-identifier"},
        {"package": "p", "typo": 1},
    ],
)
def test_bad_config_is_refused(fields):
    with pytest.raises(ValidationError):
        PackagePageConfig.model_validate(fields)


def test_a_static_app_cannot_have_a_package_page(tmp_path):
    with pytest.raises(ValidationError, match="page_from_package"):
        AppConfig(
            name="s",
            route_prefix="/api/s",
            app_type="frontend_only",
            mode="static",
            public_dir=tmp_path,
            page_from_package="pkg",
        )


def test_generic_shell_escapes_what_it_embeds():
    shell = page_shell(api="/api/x", title="</script><b>", global_name="X")
    assert "</script><b>" not in shell
    assert "\\u003c/script>" in shell  # in the JSON config
    assert "<title>&lt;/script&gt;&lt;b&gt;</title>" in shell


# -- serving ------------------------------------------------------------------


def test_generic_shell_and_assets_from_the_package(tmp_path, page_pkg):
    client = _client(tmp_path, f'page_from_package = "{PKG}"\ntitle = "Demo"\n')

    page = client.get("/demo/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-cache"
    assert '"api": "/api/demo"' in page.text and '"base": "/demo"' in page.text
    assert "window.ENLACE_TEST_PAGEPKG = " in page.text
    assert '<script src="/api/demo/ui/app.js"></script>' in page.text
    assert '<link rel="stylesheet" href="/api/demo/ui/style.css">' in page.text
    assert "<title>Demo</title>" in page.text

    # client-side routes get the same shell
    assert client.get("/demo/some/deep/route").text == page.text

    # the package's files, ahead of the app's own /ui route
    js = client.get("/api/demo/ui/app.js")
    assert js.text == "console.log('from the package');"
    assert js.headers["cache-control"] == "no-cache"
    assert client.get("/api/demo/ui/vendor/lib.js").text == "// vendored"
    assert client.get("/api/demo/ui/missing.js").status_code == 404

    # the app's API is untouched
    assert client.get("/api/demo/hello").json() == {"message": "hello"}


def test_shell_revalidates_with_an_etag(tmp_path, page_pkg):
    client = _client(tmp_path, f'page_from_package = "{PKG}"\n')
    etag = client.get("/demo/").headers["etag"]
    again = client.get("/demo/", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert client.post("/demo/").status_code == 405
    assert client.get("/demo", follow_redirects=False).status_code == 307


def test_the_apps_own_shell_function(tmp_path, page_pkg):
    client = _client(
        tmp_path,
        f"""\
        title = "Mine"
        [page_from_package]
        package = "{PKG}"
        shell = "{PKG}.web:page_shell"
        """,
    )
    assert client.get("/demo/").text == (
        "<!doctype html><p>own shell api=/api/demo base=/demo title=Mine</p>"
    )


def test_allowlist_restricts_the_files(tmp_path, page_pkg):
    client = _client(
        tmp_path,
        f"""\
        [page_from_package]
        package = "{PKG}"
        files = ["app.js", "vendor/lib.js"]
        """,
    )
    assert client.get("/api/demo/ui/app.js").status_code == 200
    assert client.get("/api/demo/ui/vendor/lib.js").status_code == 200
    assert client.get("/api/demo/ui/secret.txt").status_code == 404
    assert client.get("/api/demo/ui/style.css").status_code == 404


def test_the_page_replaces_a_frontend_dir(tmp_path, page_pkg):
    config = _platform(tmp_path, f'page_from_package = "{PKG}"\n')
    frontend = tmp_path / "apps" / "demo" / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<p>stale committed shell</p>")
    config = discover_apps(
        PlatformConfig(apps_dir=tmp_path / "apps"), on_import_error="record"
    )
    client = TestClient(build_backend(config))
    assert "window.ENLACE_TEST_PAGEPKG" in client.get("/demo/").text


def test_an_app_with_only_a_package_page_is_discovered(tmp_path, page_pkg):
    config = _platform(tmp_path, f'page_from_package = "{PKG}"\n', code="")
    (app,) = config.apps
    assert app.app_type == "frontend_only"
    client = TestClient(build_backend(config))
    assert client.get("/demo/").status_code == 200
    assert client.get("/api/demo/ui/app.js").status_code == 200


def test_the_launcher_opens_the_page(tmp_path, page_pkg):
    client = _client(tmp_path, f'page_from_package = "{PKG}"\naccess = "public"\n')
    (item,) = client.get("/_apps").json()["apps"]
    assert item["has_frontend"] is True
    assert item["launchable"] is True
    assert item["launch_url"] == "/demo/"


# -- failures are loud and local ------------------------------------------------


@pytest.mark.parametrize(
    "app_toml",
    [
        'page_from_package = "enlace_no_such_package_xyz"\n',
        f'[page_from_package]\npackage = "{PKG}"\nassets = "data/nope"\n',
        f'[page_from_package]\npackage = "{PKG}"\nshell = "{PKG}.web:missing"\n',
        f'[page_from_package]\npackage = "{PKG}"\nshell = "{PKG}.web:not_html"\n',
    ],
)
def test_a_page_that_cannot_load_stubs_its_app(tmp_path, page_pkg, app_toml):
    config = _platform(tmp_path, app_toml)
    client = TestClient(build_backend(config))
    (app,) = config.apps
    assert app.import_error is not None and app.import_error.stage == "compose"
    assert client.get("/demo/").status_code == 500
    assert client.get("/api/demo/hello").status_code == 500
    assert "demo" in client.get("/_meta").json()["degraded_apps"]


def test_fail_hard_refuses_to_boot_on_a_broken_page(tmp_path, page_pkg):
    config = _platform(
        tmp_path, 'fail_hard = true\npage_from_package = "enlace_no_such_package_xyz"\n'
    )
    with pytest.raises(ModuleNotFoundError):
        build_backend(config)
