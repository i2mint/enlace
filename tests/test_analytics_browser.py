"""Issue #55's acceptance check, in a real (headless) browser.

A page of an app with ``[analytics] mode = "privacy"`` is loaded in Chromium
from a live enlace server. The page view must be recorded, while the page
makes no request to any other origin and leaves nothing on the device: no
cookie, no localStorage, no sessionStorage, no IndexedDB.

Skipped unless Playwright and its Chromium are installed
(``pip install playwright && playwright install chromium``). CI does not
install them; run it locally when touching ``enlace.analytics``.
"""

import socket
import threading
import time

import pytest

pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from enlace import analytics as an  # noqa: E402
from enlace.base import PlatformConfig  # noqa: E402
from enlace.compose import build_backend  # noqa: E402
from enlace.discover import discover_apps  # noqa: E402

# Not "HeadlessChrome": that user agent is (rightly) classified as a bot.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port):
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return server, thread


def test_page_view_recorded_with_no_third_party_request_and_nothing_stored(tmp_path):
    app_dir = tmp_path / "apps" / "kids"
    (app_dir / "frontend").mkdir(parents=True)
    (app_dir / "frontend" / "index.html").write_text(
        '<!doctype html><html><head><meta charset="utf-8"><title>Kids</title>'
        '<link rel="stylesheet" href="style.css"></head>'
        '<body><h1>Bonjour</h1><script src="app.js"></script></body></html>'
    )
    (app_dir / "frontend" / "style.css").write_text("h1{color:teal}")
    (app_dir / "frontend" / "app.js").write_text("document.title += ' ok';")
    (app_dir / "app.toml").write_text('[analytics]\nmode = "privacy"\n')

    config = discover_apps(
        PlatformConfig(
            apps_dirs=[tmp_path / "apps"], analytics={"flush_interval_seconds": 0}
        )
    )
    store = an.JsonFileStore(tmp_path / "analytics")
    port = _free_port()
    server, thread = _serve(build_backend(config, analytics_store=store), port)
    origin = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except PlaywrightError as e:  # browser binary not installed
                pytest.skip(f"Chromium unavailable: {e}")
            context = browser.new_context(user_agent=_USER_AGENT, locale="fr-FR")
            page = context.new_page()
            requested: list[str] = []
            page.on("request", lambda r: requested.append(r.url))
            page.goto(f"{origin}/kids/", wait_until="networkidle")
            assert page.title() == "Kids ok"  # the page (and its script) ran

            storage = page.evaluate(
                """async () => ({
                    local: localStorage.length,
                    session: sessionStorage.length,
                    idb: indexedDB.databases
                        ? (await indexedDB.databases()).length : 0,
                })"""
            )
            cookies = context.cookies()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    assert requested, "the browser made no request at all?"
    assert all(url.startswith(origin + "/") for url in requested), requested
    assert cookies == []
    assert storage == {"local": 0, "session": 0, "idb": 0}

    totals = an.summarize(an.daily_counts(store, "kids", days=1))
    assert totals["pageviews"] == 1  # the page, not its CSS or JS
    assert totals["paths"] == {"/": 1}
    assert totals["languages"] == {"fr": 1}
