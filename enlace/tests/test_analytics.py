"""Privacy-first analytics (issue #55): opt-in per app, counted on the server.

The acceptance line, piece by piece:

- an app with ``[analytics] mode = "privacy"`` records a page view;
- an app without the key records nothing;
- the page is unchanged and sets no cookie (the browser-level half of that
  check — no request to another origin, nothing in storage — is
  ``tests/test_analytics_browser.py``, run in a headless browser);
- the owner can read per-path daily counts for the last 30 days.
"""

import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from enlace import analytics as an
from enlace.base import PlatformConfig
from enlace.compose import build_backend
from enlace.discover import discover_apps

BROWSER = {
    "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "sec-fetch-dest": "document",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Firefox/130.0",
    "accept-language": "fr-FR,fr;q=0.9,en;q=0.8",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _frontend_app(apps_dir: Path, name: str, analytics_mode: str = "") -> Path:
    d = apps_dir / name
    (d / "frontend" / "assets").mkdir(parents=True)
    (d / "frontend" / "index.html").write_text(
        f"<!doctype html><html><head><title>{name}</title></head>"
        f"<body>{name}</body></html>"
    )
    (d / "frontend" / "assets" / "app.css").write_text("body{}")
    if analytics_mode:
        (d / "app.toml").write_text(f'[analytics]\nmode = "{analytics_mode}"\n')
    return d


@pytest.fixture
def platform(tmp_path):
    """Two frontend apps: ``kids`` opted in, ``plain`` not. Instant flushes."""
    apps_dir = tmp_path / "apps"
    _frontend_app(apps_dir, "kids", "privacy")
    _frontend_app(apps_dir, "plain")
    store = an.JsonFileStore(tmp_path / "analytics")
    config = discover_apps(
        PlatformConfig(
            apps_dirs=[apps_dir],
            analytics={"flush_interval_seconds": 0},
        )
    )
    return config, store


def _client(config, store):
    return TestClient(build_backend(config, analytics_store=store))


def _report(store, app, days=30):
    return an.summarize(an.daily_counts(store, app, days=days))


# ---------------------------------------------------------------------------
# The acceptance line
# ---------------------------------------------------------------------------


def test_opted_in_app_records_a_page_view(platform):
    config, store = platform
    client = _client(config, store)
    assert client.get("/kids/", headers=BROWSER).status_code == 200
    totals = _report(store, "kids")
    assert totals["pageviews"] == 1
    assert totals["paths"] == {"/": 1}
    assert totals["languages"] == {"fr": 1}
    assert totals["devices"] == {"desktop": 1}
    assert totals["referrers"] == {an.DIRECT: 1}


def test_app_without_the_key_records_nothing(platform):
    config, store = platform
    client = _client(config, store)
    assert client.get("/plain/", headers=BROWSER).status_code == 200
    assert "plain" not in an.apps_with_data(store)
    assert _report(store, "plain")["pageviews"] == 0


def test_no_app_opted_in_means_no_analytics_at_all(tmp_path):
    """Not even the opt-out route or the middleware exist."""
    _frontend_app(tmp_path / "apps", "plain")
    config = discover_apps(PlatformConfig(apps_dirs=[tmp_path / "apps"]))
    store = an.JsonFileStore(tmp_path / "analytics")
    backend = build_backend(config, analytics_store=store)
    assert not hasattr(backend.state, "analytics")
    client = TestClient(backend)
    assert client.get("/plain/", headers=BROWSER).status_code == 200
    assert client.get("/_analytics/opt-out").status_code == 404
    assert list(store) == []


def test_page_is_unchanged_and_sets_no_cookie(platform, tmp_path):
    """Counting adds nothing to the response: same bytes, same headers, no cookie."""
    config, store = platform
    with_analytics = _client(config, store).get("/kids/", headers=BROWSER)
    plain_config = config.model_copy(deep=True)
    for app in plain_config.apps:
        app.analytics = an.AppAnalyticsConfig()
    without = TestClient(build_backend(plain_config)).get("/kids/", headers=BROWSER)
    assert "set-cookie" not in with_analytics.headers
    assert with_analytics.content == without.content
    assert dict(with_analytics.headers) == dict(without.headers)


def test_owner_reads_per_path_daily_counts_for_30_days(platform):
    config, store = platform
    client = _client(config, store)
    for path in ("/kids/", "/kids/lesson/1", "/kids/lesson/1?utm_source=x"):
        client.get(path, headers=BROWSER)
    series = an.daily_counts(store, "kids", days=30)
    assert len(series) == 30
    assert series[-1]["paths"] == {"/": 1, "/lesson/1": 2}  # query string dropped
    assert all(day["pageviews"] == 0 for day in series[:-1])


# ---------------------------------------------------------------------------
# What is (not) a page view
# ---------------------------------------------------------------------------


def test_assets_api_calls_and_prefetches_are_not_page_views(platform):
    config, store = platform
    client = _client(config, store)
    client.get("/kids/assets/app.css", headers={**BROWSER, "sec-fetch-dest": "style"})
    client.get("/kids/", headers={**BROWSER, "sec-fetch-dest": "empty"})  # fetch()
    client.get("/kids/", headers={**BROWSER, "sec-purpose": "prefetch"})
    client.head("/kids/", headers=BROWSER)
    client.get("/_apps", headers=BROWSER)
    assert _report(store, "kids")["pageviews"] == 0


def test_opt_out_signals_are_honoured(platform):
    config, store = platform
    client = _client(config, store)
    client.get("/kids/", headers={**BROWSER, "dnt": "1"})
    client.get("/kids/", headers={**BROWSER, "sec-gpc": "1"})
    client.get("/kids/", headers={**BROWSER, "cookie": "enlace_analytics_opt_out=1"})
    assert _report(store, "kids")["pageviews"] == 0


def test_bots_are_counted_apart(platform):
    config, store = platform
    client = _client(config, store)
    client.get("/kids/", headers={**BROWSER, "user-agent": "Googlebot/2.1"})
    totals = _report(store, "kids")
    assert totals["pageviews"] == 0
    assert totals["bot_hits"] == 1
    assert totals["devices"] == {}


def test_referrer_is_reduced_to_its_domain(platform):
    config, store = platform
    client = _client(config, store)
    client.get(
        "/kids/",
        headers={**BROWSER, "referer": "https://www.ecole.fr/classe/42?eleve=bob"},
    )
    client.get("/kids/", headers={**BROWSER, "referer": "http://testserver/kids/"})
    assert _report(store, "kids")["referrers"] == {
        "www.ecole.fr": 1,
        an.INTERNAL: 1,
    }


def test_redirects_and_errors_are_not_page_views(platform):
    """Only the page the visitor finally sees counts: a 307 hop does not."""
    config, store = platform
    client = _client(config, store)
    client.get("/kids", headers=BROWSER)  # bare prefix: 307 → /kids/
    assert _report(store, "kids")["pageviews"] == 1
    assert not an.is_page_response(404, {"content-type": "text/html"})
    assert not an.is_page_response(307, {})


def test_missing_image_is_not_a_page_view(platform):
    config, store = platform
    client = _client(config, store)
    client.get("/kids/missing.png", headers={**BROWSER, "sec-fetch-dest": "image"})
    assert _report(store, "kids")["pageviews"] == 0


def test_landing_app_gets_root_pages_but_not_platform_or_other_apps(tmp_path):
    apps_dir = tmp_path / "apps"
    _frontend_app(apps_dir, "home", "privacy")
    _frontend_app(apps_dir, "plain")
    config = discover_apps(
        PlatformConfig(
            apps_dirs=[apps_dir],
            landing_app="home",
            analytics={"flush_interval_seconds": 0},
        )
    )
    store = an.JsonFileStore(tmp_path / "analytics")
    client = _client(config, store)
    client.get("/", headers=BROWSER)
    client.get("/plain/", headers=BROWSER)  # another app, not opted in
    client.get("/_analytics/opt-out", headers=BROWSER)  # platform page
    assert _report(store, "home")["paths"] == {"/": 1}


# ---------------------------------------------------------------------------
# The opt-out page
# ---------------------------------------------------------------------------


def test_opt_out_page_sets_a_cookie_only_when_asked(platform):
    config, store = platform
    client = _client(config, store)
    page = client.get("/_analytics/opt-out", headers=BROWSER)
    assert page.status_code == 200
    assert "set-cookie" not in page.headers
    assert "Statistiques" in page.text  # French for a French browser
    assert "<script" not in page.text

    out = client.get("/_analytics/opt-out?choice=out", headers=BROWSER)
    assert "enlace_analytics_opt_out=1" in out.headers["set-cookie"]
    assert "httponly" in out.headers["set-cookie"].lower()
    client.get("/kids/", headers=BROWSER)  # the client now carries the cookie
    assert _report(store, "kids")["pageviews"] == 0

    back = client.get("/_analytics/opt-out?choice=in", headers=BROWSER)
    assert 'enlace_analytics_opt_out=""' in back.headers["set-cookie"]
    client.cookies.clear()
    client.get("/kids/", headers=BROWSER)
    assert _report(store, "kids")["pageviews"] == 1


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_app_toml_typo_fails_discovery(tmp_path):
    _frontend_app(tmp_path / "apps", "kids", "privcy")
    with pytest.raises(ValidationError, match="privacy"):
        discover_apps(PlatformConfig(apps_dirs=[tmp_path / "apps"]))


def test_retention_is_capped_at_25_months():
    with pytest.raises(ValidationError):
        PlatformConfig(analytics={"retention_days": an.MAX_RETENTION_DAYS + 1})


def test_platform_toml_analytics_table(tmp_path):
    (tmp_path / "platform.toml").write_text(
        '[analytics]\nstore_path = "data/analytics"\nretention_days = 90\n'
    )
    config = PlatformConfig.from_toml(tmp_path / "platform.toml")
    assert config.analytics.retention_days == 90
    assert config.analytics.store_path == (tmp_path / "data" / "analytics").resolve()


def test_default_store_path_is_outside_any_app(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert an.default_store_path() == tmp_path / "enlace" / "analytics"


# ---------------------------------------------------------------------------
# The counter and the store
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


DAY = 24 * 3600
T0 = 1_790_000_000  # 2026-09-21 (UTC)


def _view(counter, app="kids", path="/", device="desktop"):
    counter.record_view(
        app, path=path, referrer=an.DIRECT, language="fr", device=device
    )


def test_two_workers_write_apart_and_readers_sum_them():
    store: dict = {}
    clock = _Clock(T0)
    a = an.PageViewCounter(store, writer="a", clock=clock, flush_interval_seconds=0)
    b = an.PageViewCounter(store, writer="b", clock=clock, flush_interval_seconds=0)
    _view(a)
    _view(b)
    _view(b, path="/x")
    day = a.today()
    assert sorted(store) == [f"kids/{day}/a", f"kids/{day}/b"]
    [today] = an.daily_counts(store, "kids", days=1, today=day)
    assert today["pageviews"] == 3
    assert today["paths"] == {"/": 2, "/x": 1}


def test_flush_is_buffered_until_the_interval_elapses():
    store: dict = {}
    clock = _Clock(T0)
    c = an.PageViewCounter(store, writer="w", clock=clock, flush_interval_seconds=10)
    _view(c)
    assert store == {}
    clock.t += 10
    _view(c)
    assert store[f"kids/{c.today()}/w"]["pageviews"] == 2


def test_distinct_values_are_capped_per_dimension():
    store: dict = {}
    c = an.PageViewCounter(
        store,
        writer="w",
        clock=_Clock(T0),
        flush_interval_seconds=0,
        max_values_per_dimension=2,
    )
    for i in range(5):
        _view(c, path=f"/junk/{i}")
    assert store[f"kids/{c.today()}/w"]["paths"] == {
        "/junk/0": 1,
        "/junk/1": 1,
        an.OTHER: 3,
    }


def test_day_rollover_starts_a_new_record():
    store: dict = {}
    clock = _Clock(T0)
    c = an.PageViewCounter(store, writer="w", clock=clock, flush_interval_seconds=0)
    _view(c)
    first = c.today()
    clock.t += DAY
    _view(c)
    assert store[f"kids/{first}/w"]["pageviews"] == 1
    assert store[f"kids/{c.today()}/w"]["pageviews"] == 1


def _day(offset_days):
    return an.PageViewCounter({}, clock=_Clock(T0 + offset_days * DAY)).today()


def test_retention_window_is_exactly_retention_days():
    store = {f"kids/{_day(-30)}/w": {}, f"kids/{_day(-29)}/w": {}}
    c = an.PageViewCounter(store, retention_days=30, clock=_Clock(T0))
    assert c.purge_expired() == 1
    assert list(store) == [f"kids/{_day(-29)}/w"]


def test_timezone_decides_the_day():
    # 23:30 UTC is already the next day in Paris (UTC+2 in September).
    t = 1_790_033_400  # 2026-09-21T23:30:00Z
    utc = an.PageViewCounter({}, clock=_Clock(t)).today()
    paris = an.PageViewCounter({}, timezone="Europe/Paris", clock=_Clock(t)).today()
    assert (utc, paris) == ("2026-09-21", "2026-09-22")


def test_shutdown_flushes_the_buffer(platform):
    config, store = platform
    config.analytics.flush_interval_seconds = 3600
    with _client(config, store) as client:
        client.get("/kids/", headers=BROWSER)
        assert list(store) == []
    assert _report(store, "kids")["pageviews"] == 1


def test_a_failing_store_never_breaks_a_page(platform):
    config, _ = platform

    class Broken(dict):
        def __setitem__(self, key, value):
            raise OSError("disk full")

    client = _client(config, Broken())
    assert client.get("/kids/", headers=BROWSER).status_code == 200


def test_json_file_store_roundtrip_and_key_safety(tmp_path):
    store = an.JsonFileStore(tmp_path)
    store["kids/2026-09-21/w"] = {"pageviews": 1}
    assert store["kids/2026-09-21/w"] == {"pageviews": 1}
    assert list(store) == ["kids/2026-09-21/w"]
    del store["kids/2026-09-21/w"]
    assert list(store) == []
    assert not (tmp_path / "kids" / "2026-09-21").exists()  # emptied day removed
    for bad in ("../escape", "/abs", "a//b", "a/./b"):
        with pytest.raises(KeyError):
            store[bad] = {}


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ua, hint, expected",
    [
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Mobile/15E148", "", "mobile"),
        ("Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobile Safari", "", "mobile"),
        ("Mozilla/5.0 (Linux; Android 14; SM-X710) Safari/537.36", "", "tablet"),
        ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)", "", "tablet"),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130", "", "desktop"),
        ("Mozilla/5.0 (X11; Linux x86_64) Chrome/130", "?1", "mobile"),
        ("Mozilla/5.0 HeadlessChrome/130", "", "bot"),
        ("curl/8.4.0", "", "bot"),
        ("", "", "bot"),
    ],
)
def test_device_class(ua, hint, expected):
    assert an.device_class(ua, client_hint_mobile=hint) == expected


@pytest.mark.parametrize(
    "header, expected",
    [
        ("fr-FR,fr;q=0.9", "fr"),
        ("EN", "en"),
        ("*", an.UNKNOWN),
        ("", an.UNKNOWN),
        ("<script>", an.UNKNOWN),
    ],
)
def test_primary_language(header, expected):
    assert an.primary_language(header) == expected


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_cli_reports_per_path_daily_counts(tmp_path):
    store = an.JsonFileStore(tmp_path / "analytics")
    c = an.PageViewCounter(store, writer="w", flush_interval_seconds=0)
    _view(c, path="/")
    _view(c, path="/lesson/1")
    (tmp_path / "platform.toml").write_text('[analytics]\nstore_path = "analytics"\n')
    runner = textwrap.dedent(
        """
        import sys
        from enlace.__main__ import main
        sys.argv = ["enlace", *sys.argv[1:]]
        main()
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", runner, "analytics", "--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    assert report["kids"]["totals"]["paths"] == {"/": 1, "/lesson/1": 1}
    assert len(report["kids"]["days"]) == 30

    text = subprocess.run(
        [sys.executable, "-c", runner, "analytics"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    assert "kids: 2 page views in the last 30 days" in text
    assert "/lesson/1" in text


# ---------------------------------------------------------------------------
# Review findings: background writes, maintenance, compaction, sanitizing
# ---------------------------------------------------------------------------


def test_background_task_writes_a_lone_view_without_waiting_for_another(platform):
    """A single view reaches the store on the timer, not on the next view."""
    config, store = platform
    config.analytics.flush_interval_seconds = 0.05
    with _client(config, store) as client:
        client.get("/kids/", headers=BROWSER)
        deadline = time.time() + 5
        while not list(store) and time.time() < deadline:
            time.sleep(0.02)
        assert _report(store, "kids")["pageviews"] == 1


def test_retention_is_enforced_with_no_traffic_and_no_app_opted_in(tmp_path):
    """Old data is purged even after every app turned analytics off."""
    _frontend_app(tmp_path / "apps", "plain")
    store = an.JsonFileStore(tmp_path / "analytics")
    store["kids/2020-01-01/w"] = {"pageviews": 3}
    config = discover_apps(PlatformConfig(apps_dirs=[tmp_path / "apps"]))
    backend = build_backend(config, analytics_store=store)
    assert backend.state.analytics.collecting is False
    with TestClient(backend) as client:
        assert client.get("/_analytics/opt-out").status_code == 404
        deadline = time.time() + 5
        while list(store) and time.time() < deadline:
            time.sleep(0.02)
    assert list(store) == []


def test_compaction_folds_writers_once_and_survives_an_interrupted_run(tmp_path):
    store = an.JsonFileStore(tmp_path)
    old = _day(-3)
    store[f"kids/{old}/a"] = {"pageviews": 2, "paths": {"/": 2}}
    store[f"kids/{old}/b"] = {"pageviews": 1, "paths": {"/x": 1}}
    store[f"kids/{_day(-1)}/a"] = {"pageviews": 5}  # too recent to compact
    c = an.PageViewCounter(store, clock=_Clock(T0))
    assert c.compact(before=_day(-1)) == 2
    assert sorted(store) == sorted([f"kids/{old}/merged", f"kids/{_day(-1)}/a"])

    # A crash between writing the merged record and deleting a source must not
    # count that source twice, for readers or for the next compaction.
    store[f"kids/{old}/a"] = {"pageviews": 2, "paths": {"/": 2}}
    [day] = an.daily_counts(store, "kids", days=1, today=old)
    assert day["pageviews"] == 3
    c.compact(before=_day(-1))
    [day] = an.daily_counts(store, "kids", days=1, today=old)
    assert day["pageviews"] == 3
    assert day["paths"] == {"/": 2, "/x": 1}


def test_maintenance_lock_lets_one_worker_compact(tmp_path):
    store = an.JsonFileStore(tmp_path)
    with store.exclusive() as first:
        with an.JsonFileStore(tmp_path).exclusive() as second:
            assert (first, second) == (True, False)


def test_stale_temp_files_are_swept(tmp_path):
    store = an.JsonFileStore(tmp_path)
    (tmp_path / "kids").mkdir()
    stale = tmp_path / "kids" / ".tmp-abc.json"
    stale.write_text("{")
    os.utime(stale, (0, 0))
    assert list(store) == []  # never read as a record
    assert store.sweep_temp_files() == 1
    assert not stale.exists()


def test_app_names_outside_the_key_charset_are_stored_and_read(tmp_path):
    apps_dir = tmp_path / "apps"
    _frontend_app(apps_dir, "my app", "privacy")
    config = discover_apps(
        PlatformConfig(apps_dirs=[apps_dir], analytics={"flush_interval_seconds": 0})
    )
    store = an.JsonFileStore(tmp_path / "analytics")
    _client(config, store).get("/my app/", headers=BROWSER)
    assert an.apps_with_data(store) == ["my app"]
    assert _report(store, "my app")["pageviews"] == 1


def test_scanner_probes_count_as_bot_hits_not_paths(platform):
    config, store = platform
    client = _client(config, store)
    for probe in ("/kids/wp-admin/setup.php", "/kids/.env", "/kids/.git/config"):
        client.get(probe, headers=BROWSER)
    client.get("/kids/lesson/1", headers=BROWSER)
    client.get("/kids/index.html", headers=BROWSER)
    totals = _report(store, "kids")
    assert totals["bot_hits"] == 3
    assert totals["paths"] == {"/lesson/1": 1, "/": 1}


@pytest.mark.parametrize(
    "raw, stored",
    [
        ("/u/alice@example.com/reset", "/u/:id/reset"),
        ("/doc/3f2b8c1e-9a4d-4e21-b7c3-0d5e6f7a8b9c", "/doc/:id"),
        ("/share/aZ9kQ2xLm8Rt4Vb7Np", "/share/:id"),
        ("/lesson/12/fractions-intro", "/lesson/12/fractions-intro"),
        ("/\x1b[31mRED\x1b[0m", "/[31mRED[0m"),
    ],
)
def test_stored_paths_are_redacted_and_printable(raw, stored):
    assert an.normalize_path(raw) == stored


@pytest.mark.parametrize(
    "referer, expected",
    [
        ("http://203.0.113.7/x", an.IP),
        ("http://[2001:db8::1]/x", an.IP),
        ("https://<img src=x onerror=alert(1)>/", an.UNKNOWN),
        ("https://École.fr/", "xn--cole-9oa.fr"),
    ],
)
def test_referrer_hosts_are_plain_names_or_placeholders(referer, expected):
    assert an.referrer_domain(referer, host="kids.example") == expected


def test_unknown_timezone_is_a_config_error_not_a_boot_crash():
    with pytest.raises(ValidationError, match="timezone"):
        PlatformConfig(analytics={"timezone": "Europe/Pariss"})


def test_platform_paths_never_count_even_under_an_app_mounted_at_root(tmp_path):
    apps_dir = tmp_path / "apps"
    d = _frontend_app(apps_dir, "site")
    (d / "app.toml").write_text('route = "/"\n[analytics]\nmode = "privacy"\n')
    config = discover_apps(PlatformConfig(apps_dirs=[apps_dir]))
    attribute = an.PageAttribution(
        config.apps, exclude_prefixes=config.analytics.exclude_prefixes
    )
    assert attribute("/_analytics/opt-out") is None
    assert attribute("/auth/login") is None
    assert attribute("/about") == ("site", "/about")


def test_writer_id_follows_the_process():
    """Workers forked from one preloaded app must not share a writer id."""
    c = an.PageViewCounter({})
    assert c.writer.startswith(f"{os.getpid()}-")
    assert c.writer == c.writer


def test_a_broken_store_logs_once_not_on_every_flush(caplog):
    class Broken(dict):
        def __setitem__(self, key, value):
            raise OSError("disk full")

    c = an.PageViewCounter(Broken(), writer="w", flush_interval_seconds=0)
    with caplog.at_level(logging.WARNING, logger="enlace.analytics"):
        for _ in range(5):
            _view(c)
    assert len([r for r in caplog.records if "could not write" in r.message]) == 1
