"""Tests for enlace.ratelimit: per-app, per-client limits declared in app.toml."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from enlace.ratelimit import (
    Rate,
    RateLimitMiddleware,
    TokenBucketLimiter,
    client_address,
    parse_networks,
    parse_rate,
    parse_rule_key,
    rules_for_app,
    validate_rate_limits,
)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize(
    "text, rate",
    [
        ("60/minute", Rate(60, 60)),
        ("60/minutes", Rate(60, 60)),
        ("5/s", Rate(5, 1)),
        ("100 / hour", Rate(100, 3600)),
        ("1/day", Rate(1, 86400)),
    ],
)
def test_parse_rate(text, rate):
    assert parse_rate(text) == rate


@pytest.mark.parametrize("text", ["60", "0/minute", "60/fortnight", "x/minute", ""])
def test_parse_rate_rejects_nonsense(text):
    with pytest.raises(ValueError):
        parse_rate(text)


@pytest.mark.parametrize(
    "key", ["attempts", "FETCH /x", "POST /x extra", "POST attempts"]
)
def test_parse_rule_key_rejects_nonsense(key):
    with pytest.raises(ValueError):
        parse_rule_key(key)


def test_star_method_means_any():
    assert parse_rule_key("* /x") == (None, "/x")


def test_validate_rate_limits_reports_the_bad_entry():
    with pytest.raises(ValueError, match="fortnight"):
        validate_rate_limits({"POST /a": "1/minute", "/b": "1/fortnight"})


def test_token_bucket_allows_a_burst_then_refills():
    clock = _Clock()
    lim = TokenBucketLimiter(Rate(3, 60), clock=clock)
    assert [lim.acquire("k") for _ in range(3)] == [0, 0, 0]
    wait = lim.acquire("k")
    assert wait == pytest.approx(20.0)  # one token per 20 s
    assert lim.acquire("other") == 0  # buckets are per key
    clock.now += 20
    assert lim.acquire("k") == 0
    assert lim.acquire("k") > 0


def test_token_bucket_is_bounded_in_keys():
    lim = TokenBucketLimiter(Rate(1, 60), max_keys=2, clock=_Clock())
    for key in ("a", "b", "c"):
        lim.acquire(key)
    assert list(lim._buckets) == ["b", "c"]


def test_forwarded_for_is_ignored_from_an_untrusted_peer():
    trusted = parse_networks(["127.0.0.1", "::1"])
    assert client_address("9.9.9.9", "1.2.3.4", trusted=trusted) == "9.9.9.9"
    assert client_address("127.0.0.1", "1.2.3.4", trusted=trusted) == "1.2.3.4"
    # A client-sent header is prepended to, not trusted: rightmost untrusted hop wins.
    assert client_address("127.0.0.1", "6.6.6.6, 1.2.3.4", trusted=trusted) == "1.2.3.4"
    assert client_address("127.0.0.1", "1.2.3.4", trusted=[]) == "127.0.0.1"


def _app(rules_table, *, trusted_proxies=(), prefix="/api/game"):
    inner = FastAPI()

    @inner.post("/attempts")
    def attempts():
        return {"ok": True}

    @inner.get("/attempts")
    def list_attempts():
        return {"ok": True}

    outer = FastAPI()
    outer.mount(prefix, inner)
    rules = list(rules_for_app("game", prefix, rules_table))
    return RateLimitMiddleware(outer, rules=rules, trusted_proxies=trusted_proxies)


def test_middleware_answers_429_with_retry_after():
    client = TestClient(_app({"POST /attempts": "2/minute"}))
    assert client.post("/api/game/attempts").status_code == 200
    assert client.post("/api/game/attempts").status_code == 200
    r = client.post("/api/game/attempts")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1
    assert r.json()["app"] == "game"
    # Other methods and paths are untouched.
    assert client.get("/api/game/attempts").status_code == 200


def test_middleware_keys_on_the_forwarded_client_behind_a_trusted_proxy():
    app = _app({"POST /attempts": "1/minute"}, trusted_proxies=["127.0.0.1"])
    client = TestClient(app, client=("127.0.0.1", 50000))
    a = {"x-forwarded-for": "1.1.1.1"}
    b = {"x-forwarded-for": "2.2.2.2"}
    assert client.post("/api/game/attempts", headers=a).status_code == 200
    assert client.post("/api/game/attempts", headers=a).status_code == 429
    assert client.post("/api/game/attempts", headers=b).status_code == 200


def test_trusting_every_address_falls_back_to_the_peer():
    trusted = parse_networks(["0.0.0.0/0"])
    assert client_address("10.0.0.1", "1.1.1.1", trusted=trusted) == "10.0.0.1"


def test_middleware_ignores_forwarded_for_without_trusted_proxies():
    client = TestClient(_app({"POST /attempts": "1/minute"}))
    assert (
        client.post(
            "/api/game/attempts", headers={"x-forwarded-for": "1.1.1.1"}
        ).status_code
        == 200
    )
    # Spoofing a new address does not buy a new bucket.
    assert (
        client.post(
            "/api/game/attempts", headers={"x-forwarded-for": "2.2.2.2"}
        ).status_code
        == 429
    )


def test_glob_rules_and_any_method():
    client = TestClient(_app({"/att*": "1/minute"}))
    assert client.get("/api/game/attempts").status_code == 200
    assert client.post("/api/game/attempts").status_code == 429


def test_build_backend_installs_limits_from_app_config(tmp_path):
    from enlace.base import AppConfig, PlatformConfig
    from enlace.compose import build_backend

    app_dir = tmp_path / "game"
    app_dir.mkdir()
    (app_dir / "server.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.post('/attempts')\n"
        "def attempts():\n"
        "    return {'ok': True}\n"
    )
    app_cfg = AppConfig(
        name="game",
        route_prefix="/api/game",
        entry_module_path=app_dir / "server.py",
        app_type="asgi_app",
        access="public",
        rate_limit={"POST /attempts": "1/minute"},
    )
    config = PlatformConfig(apps=[app_cfg], trusted_proxies=["127.0.0.1"])
    client = TestClient(build_backend(config))
    assert client.post("/api/game/attempts").status_code == 200
    assert client.post("/api/game/attempts").status_code == 429


def test_app_config_rejects_a_bad_rate_limit():
    from enlace.base import AppConfig

    with pytest.raises(ValueError):
        AppConfig(
            name="game",
            route_prefix="/api/game",
            app_type="asgi_app",
            rate_limit={"POST /attempts": "lots"},
        )


def test_platform_config_rejects_a_bad_trusted_proxy():
    from enlace.base import PlatformConfig

    with pytest.raises(ValueError):
        PlatformConfig(trusted_proxies=["not-an-ip"])


def _game_app_dir(apps_dir, app_toml):
    d = apps_dir / "game"
    d.mkdir(parents=True)
    (d / "server.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.post('/attempts')\n"
        "def attempts():\n"
        "    return {'ok': True}\n"
    )
    (d / "app.toml").write_text(app_toml)
    return d


def test_app_toml_rate_limit_is_discovered_and_enforced(tmp_path):
    from enlace.base import PlatformConfig
    from enlace.compose import build_backend
    from enlace.discover import discover_apps

    _game_app_dir(
        tmp_path / "apps",
        'access = "public"\n[rate_limit]\n"POST /attempts" = "1/minute"\n',
    )
    config = discover_apps(PlatformConfig(apps_dirs=[tmp_path / "apps"]))
    (game,) = config.apps
    assert game.rate_limit == {"POST /attempts": "1/minute"}
    client = TestClient(build_backend(config))
    url = f"{game.route_prefix}/attempts"
    assert client.post(url).status_code == 200
    assert client.post(url).status_code == 429


def test_app_toml_bad_rate_limit_fails_discovery(tmp_path):
    from enlace.base import PlatformConfig
    from enlace.discover import discover_apps

    _game_app_dir(tmp_path / "apps", '[rate_limit]\n"POST /attempts" = "lots"\n')
    with pytest.raises(ValueError, match="lots"):
        discover_apps(PlatformConfig(apps_dirs=[tmp_path / "apps"]))


# --- review findings: bypasses and edge cases --------------------------------


def test_dot_segments_do_not_sidestep_a_rule():
    client = TestClient(_app({"POST /attempts": "1/minute"}))
    assert client.post("/api/game/attempts").status_code == 200
    from enlace.ratelimit import normalize_path

    for sneaky in ("/api/game/x/../attempts", "/api/game/./attempts"):
        assert normalize_path(sneaky) == "/api/game/attempts"
    # Starlette itself does not resolve them, so drive the middleware directly.
    rule = next(rules_for_app("game", "/api/game", {"POST /attempts": "1/minute"}))
    assert rule.matches("POST", normalize_path("//api/game//x/../attempts"))


def test_head_counts_against_a_get_rule():
    client = TestClient(_app({"GET /attempts": "1/minute"}))
    client.head("/api/game/attempts")  # whatever the app answers, it is counted
    assert client.get("/api/game/attempts").status_code == 429


@pytest.mark.parametrize("key", ["POST /a%20b", "POST /a/../b", "POST //a"])
def test_rule_keys_must_be_decoded_and_normalised(key):
    with pytest.raises(ValueError):
        parse_rule_key(key)


def test_forwarded_hops_with_ports_and_mapped_peers():
    trusted = parse_networks(["127.0.0.1"])
    assert client_address("::ffff:127.0.0.1", "1.2.3.4", trusted=trusted) == "1.2.3.4"
    assert client_address("127.0.0.1", "1.2.3.4:5678", trusted=trusted) == "1.2.3.4"
    assert client_address("127.0.0.1", "[2001:db8::1]:80", trusted=trusted) == (
        "2001:db8::1"
    )
    # Garbage in the chain: fall back to the peer, never a raw string key.
    assert client_address("127.0.0.1", "not-an-ip", trusted=trusted) == "127.0.0.1"


def _platform_with_cors(tmp_path):
    from enlace.base import PlatformConfig
    from enlace.compose import build_backend
    from enlace.discover import discover_apps

    _game_app_dir(
        tmp_path / "apps", 'access = "public"\n[rate_limit]\n"/attempts" = "1/minute"\n'
    )
    config = discover_apps(
        PlatformConfig(
            apps_dirs=[tmp_path / "apps"], cors_origins=["https://ui.example.com"]
        )
    )
    return TestClient(build_backend(config)), f"{config.apps[0].route_prefix}/attempts"


def test_preflights_are_not_counted_and_429_carries_cors(tmp_path):
    client, url = _platform_with_cors(tmp_path)
    origin = {"origin": "https://ui.example.com"}
    preflight = {**origin, "access-control-request-method": "POST"}
    for _ in range(3):
        assert client.options(url, headers=preflight).status_code == 200
    assert client.post(url, headers=origin).status_code == 200
    r = client.post(url, headers=origin)
    assert r.status_code == 429
    assert r.headers["access-control-allow-origin"] == "https://ui.example.com"


def test_websocket_handshakes_are_limited():
    from starlette.websockets import WebSocket, WebSocketDisconnect

    inner = FastAPI()

    @inner.websocket("/live")
    async def live(ws: WebSocket):
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()

    outer = FastAPI()
    outer.mount("/api/game", inner)
    rules = list(rules_for_app("game", "/api/game", {"/live": "1/minute"}))
    client = TestClient(RateLimitMiddleware(outer, rules=rules))
    with client.websocket_connect("/api/game/live") as ws:
        assert ws.receive_text() == "hi"
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/game/live") as ws:
            ws.receive_text()
    assert exc.value.code == 1008
