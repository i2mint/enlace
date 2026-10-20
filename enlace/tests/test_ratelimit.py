"""Tests for enlace.ratelimit: per-app, per-client limits declared in app.toml.

Also the one home of the shared client-address and limiter tests: heed's public
ingest endpoint uses the same implementation through thin aliases.
"""

import ipaddress
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from enlace.ratelimit import (
    Rate,
    RateLimitMiddleware,
    RateLimitRule,
    RedisLimiter,
    SlidingWindowLimiter,
    TokenBucketLimiter,
    add_rate_limits,
    bucket_key,
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


# ------------------------------------------------------- shared limiters (heed too)


def test_sliding_window_has_no_refill_until_the_oldest_hit_leaves():
    clock = _Clock()
    lim = SlidingWindowLimiter(Rate(2, 10), clock=clock)
    assert [lim.acquire("k") for _ in range(2)] == [0, 0]
    assert lim.acquire("k") == pytest.approx(10.0)
    clock.now += 5
    assert lim.acquire("k") == pytest.approx(5.0)  # a bucket would have refilled
    clock.now += 5
    assert lim.allow("k") and lim.allow("k") and not lim.allow("k")


def test_sliding_window_is_bounded_in_keys():
    lim = SlidingWindowLimiter(Rate(1, 60), max_keys=2, clock=_Clock())
    for key in ("a", "b", "a", "c"):
        lim.acquire(key)
    assert list(lim._hits) == ["a", "c"]  # "b" was the least recently seen


@pytest.mark.parametrize("cls", [TokenBucketLimiter, SlidingWindowLimiter])
def test_in_memory_limiters_reject_a_zero_key_bound(cls):
    with pytest.raises(ValueError, match="max_keys"):
        cls(Rate(1, 1), max_keys=0)


class _FakePipe:
    def __init__(self, store, fail):
        self.store, self.fail, self.ops, self.ttl = store, fail, [], None

    def incr(self, key):
        self.ops.append(key)

    def expire(self, key, seconds):
        self.ttl = seconds

    def execute(self):
        if self.fail:
            raise ConnectionError("redis down")
        key = self.ops[0]
        self.store[key] = self.store.get(key, 0) + 1
        return [self.store[key], True]


class _FakeRedis:
    def __init__(self, fail=False):
        self.store: dict = {}
        self.fail = fail

    def pipeline(self):
        return _FakePipe(self.store, self.fail)


def test_redis_limiter_counts_per_window_shared_across_instances():
    redis, now = _FakeRedis(), [0.0]
    a = RedisLimiter(redis, Rate(2, 60), clock=lambda: now[0])
    b = RedisLimiter(redis, Rate(2, 60), clock=lambda: now[0])
    assert a.acquire("k") == 0 and b.acquire("k") == 0  # one shared count
    now[0] = 45.0
    assert a.acquire("k") == pytest.approx(15.0)  # until the next window
    now[0] = 61.0
    assert b.allow("k")  # new window
    assert all(key.startswith("enlace:rl:60:k:") for key in redis.store)


def test_redis_limiter_falls_back_when_redis_down(caplog):
    lim = RedisLimiter(_FakeRedis(fail=True), Rate(1, 60))
    assert isinstance(lim.fallback, TokenBucketLimiter)
    with caplog.at_level(logging.WARNING, logger="enlace.ratelimit"):
        assert lim.allow("k") and not lim.allow("k")
    assert caplog.text.count("unavailable") == 1  # once per outage, not per request


def test_redis_limiter_skips_redis_while_down_then_retries():
    redis, now = _FakeRedis(fail=True), [0.0]
    calls = []
    pipeline = redis.pipeline
    redis.pipeline = lambda: calls.append(1) or pipeline()
    lim = RedisLimiter(redis, Rate(5, 60), retry_seconds=5, clock=lambda: now[0])
    lim.acquire("k"), lim.acquire("k")
    assert len(calls) == 1  # the second request never waited on a dead Redis
    now[0] = 5.0
    redis.fail = False
    assert lim.acquire("k") == 0 and len(calls) == 2 and lim._down_until is None


def test_two_windows_never_share_a_redis_counter():
    redis = _FakeRedis()
    RedisLimiter(redis, Rate(1, 60), clock=lambda: 0.0).acquire("k")
    assert RedisLimiter(redis, Rate(1, 3600), clock=lambda: 0.0).allow("k")


@pytest.mark.parametrize("limit, window", [(0, 60), (5, 0), (5, -1), (1, float("inf"))])
def test_rate_rejects_limits_that_could_never_be_enforced(limit, window):
    with pytest.raises(ValueError, match="Rate needs"):
        Rate(limit, window)


def test_ipv6_prefix_is_validated():
    with pytest.raises(ValueError, match="ipv6_prefix"):
        RateLimitMiddleware(FastAPI(), rules=[], ipv6_prefix=200)


def test_ipv6_zone_ids_are_dropped_from_forwarded_hops():
    trusted = parse_networks(["127.0.0.1"])
    hop = "fe80::1%" + "x" * 500
    assert client_address("127.0.0.1", hop, trusted=trusted) == "fe80::1"


def test_blocking_limiters_run_off_the_event_loop():
    import threading

    loop_thread, limiter_thread = [], []

    class Spy(SlidingWindowLimiter):
        blocking = True  # as RedisLimiter: does network I/O in acquire

        def acquire(self, key):
            limiter_thread.append(threading.get_ident())
            return super().acquire(key)

    inner = FastAPI()

    @inner.get("/a")
    async def ok():
        loop_thread.append(threading.get_ident())
        return {"ok": True}

    outer = FastAPI()
    outer.mount("/api/game", inner)
    rules = list(rules_for_app("game", "/api/game", {"/a": "1/minute"}))
    client = TestClient(RateLimitMiddleware(outer, rules=rules, make_limiter=Spy))
    assert client.get("/api/game/a").status_code == 200
    assert client.get("/api/game/a").status_code == 429  # its result still counts
    assert limiter_thread[0] != loop_thread[0]


def test_redis_limiter_uses_an_injected_fallback():
    fallback = SlidingWindowLimiter(Rate(1, 60))
    lim = RedisLimiter(_FakeRedis(fail=True), Rate(5, 60), fallback=fallback)
    assert lim.allow("k") and not lim.allow("k")
    assert list(fallback._hits) == ["k"]


def test_redis_from_url_sets_socket_timeouts(monkeypatch):
    redis = pytest.importorskip("redis")
    seen = {}

    def fake_from_url(url, **kw):
        seen.update(kw)
        return _FakeRedis()

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(fake_from_url))
    lim = RedisLimiter.from_url("redis://x:6379/0", Rate(1, 1), namespace="ns")
    assert seen["socket_timeout"] == seen["socket_connect_timeout"] == 0.5
    assert lim.namespace == "ns" and lim.rate == Rate(1, 1)


def test_one_shared_limiter_keeps_rules_apart():
    """A shared (e.g. Redis) limiter behind make_limiter= must not mix two rules."""
    shared = SlidingWindowLimiter(Rate(1, 60))
    inner = FastAPI()

    @inner.get("/a")
    @inner.get("/b")
    def ok():
        return {"ok": True}

    outer = FastAPI()
    outer.mount("/api/game", inner)
    rules = list(rules_for_app("game", "/api/game", {"/a": "1/minute", "/b": "1/m"}))
    client = TestClient(
        RateLimitMiddleware(outer, rules=rules, make_limiter=lambda rate: shared)
    )
    assert client.get("/api/game/a").status_code == 200
    assert client.get("/api/game/b").status_code == 200  # its own count
    assert client.get("/api/game/a").status_code == 429
    assert len(shared._hits) == 2


def test_parse_networks_accepts_parsed_networks():
    net = ipaddress.ip_network("10.0.0.0/8")
    assert parse_networks([net, "::1"]) == [net, ipaddress.ip_network("::1/128")]
    with pytest.raises(ValueError):
        parse_networks(["not-a-network"])


def test_bucket_key_groups_ipv6_and_unwraps_mapped_ipv4():
    assert bucket_key("2001:db8:1:2::1") == bucket_key("2001:db8:1:2:ffff::9")
    assert bucket_key("2001:db8:1:2::1") != bucket_key("2001:db8:1:3::1")
    assert bucket_key("::ffff:1.2.3.4") == "1.2.3.4"
    assert bucket_key("2001:db8::1", ipv6_prefix=128) == "2001:db8::1/128"
    assert bucket_key("unknown") == "unknown"


# --- #100: unix-socket peers, eviction policy, fail-closed option ---


def test_a_missing_peer_is_trusted_only_with_trust_unix_socket():
    assert client_address(None, "1.2.3.4", trusted=[]) == "unknown"
    assert client_address(None, "1.2.3.4", trusted=[], trust_unix_socket=True) == (
        "1.2.3.4"
    )
    nets = parse_networks(["10.0.0.0/8"])
    # Trusted hops are skipped as usual behind the socket.
    assert (
        client_address(None, "1.2.3.4, 10.0.0.2", trusted=nets, trust_unix_socket=True)
        == "1.2.3.4"
    )
    # A peer that is present and untrusted is still not trusted.
    assert client_address("5.5.5.5", "1.2.3.4", trusted=[], trust_unix_socket=True) == (
        "5.5.5.5"
    )


async def _call_with_no_peer(mw, forwarded):
    sent = []

    async def receive():  # pragma: no cover - the app never reads the body
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": [(b"x-forwarded-for", forwarded.encode())],
        "client": None,
    }
    await mw(scope, receive, send)
    return sent[0]["status"]


def _ok_app():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app


@pytest.mark.parametrize("trust, second_status", [(False, 429), (True, 200)])
def test_unix_socket_clients_get_their_own_buckets_only_when_trusted(
    trust, second_status, caplog
):
    import asyncio

    rule = RateLimitRule("a", "GET", "/x", Rate(1, 60), "GET /x = 1/minute")
    with caplog.at_level(logging.WARNING, logger="enlace.ratelimit"):
        mw = RateLimitMiddleware(_ok_app(), rules=[rule], trust_unix_socket=trust)
        first = asyncio.run(_call_with_no_peer(mw, "1.1.1.1"))
        second = asyncio.run(_call_with_no_peer(mw, "2.2.2.2"))
        asyncio.run(_call_with_no_peer(mw, "3.3.3.3"))
    assert (first, second) == (200, second_status)
    if trust:  # warned once at startup that the socket is trusted
        assert caplog.text.count("unix sockets") == 1
    else:  # warned once, not per request, that everyone shares one bucket
        assert caplog.text.count("'unknown' bucket") == 1


def test_platform_trust_unix_socket_reaches_the_middleware():
    from types import SimpleNamespace

    from enlace.base import PlatformConfig

    assert PlatformConfig().trust_unix_socket is False
    installed = {}

    class _Parent:
        def add_middleware(self, cls, **kwargs):
            installed.update(kwargs)

    app = SimpleNamespace(
        name="a", route_prefix="/api/a", rate_limit={"/x": "1/minute"}
    )
    config = SimpleNamespace(apps=[app], trusted_proxies=[], trust_unix_socket=True)
    assert add_rate_limits(_Parent(), config)
    assert installed["trust_unix_socket"] is True


@pytest.mark.parametrize("cls", [TokenBucketLimiter, SlidingWindowLimiter])
def test_idle_keys_are_evicted_before_busy_ones(cls):
    clock = _Clock()
    lim = cls(Rate(1, 10), max_keys=2, clock=clock)
    lim.acquire("busy")  # exhausted
    clock.now += 5
    lim.acquire("idle-soon")
    clock.now += 6  # "busy" is idle again (11 s > 10 s); "idle-soon" is not
    lim.acquire("new")
    entries = lim._buckets if cls is TokenBucketLimiter else lim._hits
    assert list(entries) == ["idle-soon", "new"]


@pytest.mark.parametrize("cls", [TokenBucketLimiter, SlidingWindowLimiter])
def test_rotating_keys_cannot_evict_an_exhausted_client_under_deny(cls):
    clock = _Clock()
    lim = cls(Rate(1, 60), max_keys=2, on_overflow="deny", clock=clock)
    assert lim.allow("victim") and not lim.allow("victim")  # exhausted
    assert lim.allow("rot-0")
    for i in range(1, 10):  # a flood of new keys is refused, not admitted
        assert lim.acquire(f"rot-{i}") > 0
    assert not lim.allow("victim")  # its exhausted bucket was kept
    clock.now += 61  # both tracked keys are idle now: room again
    assert lim.allow("rot-new")


@pytest.mark.parametrize("cls", [TokenBucketLimiter, SlidingWindowLimiter])
def test_eviction_of_busy_keys_is_warned_once(cls, caplog):
    lim = cls(Rate(1, 60), max_keys=1, clock=_Clock())
    with caplog.at_level(logging.WARNING, logger="enlace.ratelimit"):
        for key in "abcd":
            lim.acquire(key)
    assert caplog.text.count("evicting the least recently seen") == 1


def test_a_full_limiter_sweeps_at_most_once_per_min_sweep_seconds():
    clock = _Clock()
    lim = TokenBucketLimiter(Rate(1, 60), max_keys=1, on_overflow="deny", clock=clock)
    lim.acquire("a")
    calls = []
    original = lim._idle_in
    lim._idle_in = lambda entry, now: calls.append(now) or original(entry, now)
    for i in range(5):
        lim.acquire(f"new-{i}")
    assert len(calls) == 1  # one sweep, then refused from the remembered horizon


@pytest.mark.parametrize("bad", [{"max_keys": 0}, {"on_overflow": "drop"}])
@pytest.mark.parametrize("cls", [TokenBucketLimiter, SlidingWindowLimiter])
def test_in_memory_limiters_validate_their_bound_options(cls, bad):
    with pytest.raises(ValueError):
        cls(Rate(1, 1), **bad)


def test_redis_on_error_deny_refuses_until_redis_is_retried(caplog):
    now = [0.0]
    lim = RedisLimiter(
        _FakeRedis(fail=True),
        Rate(5, 60),
        on_error="deny",
        retry_seconds=5,
        clock=lambda: now[0],
    )
    with caplog.at_level(logging.WARNING, logger="enlace.ratelimit"):
        assert lim.acquire("k") == pytest.approx(5.0)
        now[0] = 2.0
        assert lim.acquire("k") == pytest.approx(3.0)
    assert "refusing every request" in caplog.text


def test_redis_on_error_deny_with_no_retry_delay_still_refuses():
    lim = RedisLimiter(
        _FakeRedis(fail=True), Rate(5, 60), on_error="deny", retry_seconds=0
    )
    assert lim.acquire("k") == 60


def test_redis_on_error_allow_admits_everything():
    lim = RedisLimiter(_FakeRedis(fail=True), Rate(1, 60), on_error="allow")
    assert all(lim.allow("k") for _ in range(5))


def test_redis_on_error_is_validated():
    with pytest.raises(ValueError, match="on_error"):
        RedisLimiter(_FakeRedis(), Rate(1, 60), on_error="ignore")


def test_an_async_redis_client_is_refused_up_front():
    class _AsyncRedis:
        async def execute_command(self, *args):  # pragma: no cover - never awaited
            return 1

        def pipeline(self):  # pragma: no cover
            return None

    with pytest.raises(TypeError, match="synchronous redis client"):
        RedisLimiter(_AsyncRedis(), Rate(1, 60))
