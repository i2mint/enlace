"""Opt-in, per-app, per-client rate limits for mounted apps.

An app declares limits in its ``app.toml``; enlace enforces them in front of the
app, so the app needs no code (and still runs alone without them)::

    [rate_limit]
    "POST /attempts" = "60/minute"      # method + path under the app's API prefix
    "/search" = "10/second"             # no method: any method
    "POST /items/*" = "100/hour"        # fnmatch-style glob

A request over its limit gets ``429 Too Many Requests`` with ``Retry-After``.

**Who the client is.** Behind a reverse proxy the TCP peer is the proxy, so the
client address comes from ``X-Forwarded-For`` -- but only when the peer is one of
platform.toml's ``[platform] trusted_proxies``, and read right to left skipping
trusted hops, so a client cannot spoof it by sending the header itself. With no
``trusted_proxies`` the peer address in the ASGI scope is used as is (which is
already the client when the server itself resolves proxy headers, as uvicorn does
for a loopback proxy by default). IPv6 clients share a bucket per /64.

**What is matched.** The request path is normalised first (``//``, ``.`` and
``..`` collapsed, as an upstream would resolve them), so a rule cannot be
sidestepped with ``/a/../attempts``. A ``GET`` rule also covers ``HEAD``. CORS
preflights (``OPTIONS`` with ``Access-Control-Request-Method``) are never counted.
A websocket handshake is matched as ``GET`` and refused with close code 1008.

**Scope of the guarantee.** Buckets are in memory, per process: a host running N
worker processes allows up to N times the declared rate. Each rule tracks at most
``max_keys`` clients and evicts the least recently seen, and an evicted bucket
comes back full, so a client rotating through more addresses than that (e.g. many
IPv6 /64s) is not held to the rate. It bounds abuse; it is not an exact quota. The
limiter is a seam (``make_limiter``) for a shared one.

**In production behind a proxy, set ``trusted_proxies``** unless the server
already resolves the client from the proxy's header: otherwise every client
shares the proxy's address, and one abusive client exhausts everyone's bucket.

**Reusable on its own.** The client-address and limiter halves import nothing
beyond the standard library, so other services use them directly (``heed``'s
public ingest endpoint does): :func:`client_address` + :func:`bucket_key` say
who the client is, and any :class:`Limiter` -- :class:`TokenBucketLimiter`,
:class:`SlidingWindowLimiter`, or :class:`RedisLimiter` (one count shared by every
worker and host; needs the ``[redis]`` extra) -- says whether it may proceed. To
share enlace's own limits across workers, pass the Redis one to the seam::

    RateLimitMiddleware(app, rules=rules, make_limiter=lambda rate: RedisLimiter(
        client, rate))

Each rule charges its own key (the rule's :attr:`RateLimitRule.id` plus the
client), so one shared client never mixes two rules' counts.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import logging
import math
import posixpath
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger("enlace.ratelimit")

#: Window lengths, in seconds, a rate may be expressed in ("60/minute").
RATE_UNITS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "h": 3600,
    "hour": 3600,
    "d": 86400,
    "day": 86400,
}
#: Client buckets one rule tracks before evicting the least recently seen, so a
#: flood of distinct (spoofed or rotating) clients cannot exhaust memory.
DEFAULT_MAX_KEYS = 100_000
#: IPv6 clients are grouped by this prefix: one host usually owns a whole /64.
DEFAULT_IPV6_PREFIX = 64
#: Connect/read timeout of the Redis limiter: a hung Redis costs at most this long.
DEFAULT_REDIS_SOCKET_TIMEOUT = 0.5
#: After a Redis error, the Redis limiter uses its fallback this long before retrying,
#: so an outage costs one timeout per this interval, not one per request.
DEFAULT_REDIS_RETRY_SECONDS = 5.0

_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*([a-z]+?)s?\s*$")
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


@dataclass(frozen=True)
class Rate:
    """``limit`` requests per ``window`` seconds (``limit >= 1``, ``window > 0``).

    >>> Rate(0, 60)
    Traceback (most recent call last):
    ...
    ValueError: Rate needs limit >= 1 and a finite window > 0, got Rate(0, 60)
    """

    limit: int
    window: float

    def __post_init__(self):
        ok_window = isinstance(self.window, (int, float)) and math.isfinite(self.window)
        if not (
            isinstance(self.limit, int)
            and self.limit >= 1
            and ok_window
            and self.window > 0
        ):
            raise ValueError(
                f"Rate needs limit >= 1 and a finite window > 0, "
                f"got Rate({self.limit!r}, {self.window!r})"
            )


@dataclass(frozen=True)
class RateLimitRule:
    """One declared limit: requests matching ``method`` + ``path`` share ``rate``.

    ``method`` is ``None`` for any method; ``path`` is a full request path (or an
    fnmatch glob) and ``spec`` is the app.toml text it came from, for messages.
    """

    app: str
    method: str | None
    path: str
    rate: Rate
    spec: str

    def matches(self, method: str, path: str) -> bool:
        """Whether a request matches; *path* should be :func:`normalize_path` ed.

        >>> r = RateLimitRule("a", "GET", "/x", Rate(1, 1), "")
        >>> r.matches("HEAD", "/x"), r.matches("POST", "/x")
        (True, False)
        """
        if self.method is not None and method != self.method:
            if not (self.method == "GET" and method == "HEAD"):
                return False
        return fnmatch.fnmatchcase(path, self.path)

    @property
    def id(self) -> str:
        """Names the rule across processes: the prefix of every key it charges.

        >>> RateLimitRule("a", "GET", "/api/a/x", Rate(1, 1), "GET /x = 1/s").id
        'a:GET /x = 1/s'
        """
        return f"{self.app}:{self.spec}"


def normalize_path(path: str) -> str:
    """Collapse ``//``, ``.`` and ``..`` the way an upstream would resolve them.

    >>> normalize_path("/api/x/foo/../attempts"), normalize_path("//api/./x//a/")
    ('/api/x/attempts', '/api/x/a/')
    """
    norm = posixpath.normpath("/" + path.lstrip("/"))
    if norm.startswith("//"):  # normpath keeps a leading double slash
        norm = "/" + norm.lstrip("/")
    if path.endswith("/") and norm != "/":
        norm += "/"
    return norm


def parse_rate(text: str) -> Rate:
    """Parse ``"60/minute"`` (also ``60/min``, ``60/m``, ``5/seconds``...).

    >>> parse_rate("60/minute")
    Rate(limit=60, window=60)
    >>> parse_rate("10 / s")
    Rate(limit=10, window=1)
    """
    m = _RATE_RE.match(str(text).lower())
    if not m or m.group(2) not in RATE_UNITS:
        raise ValueError(
            f"invalid rate {text!r}: expected '<count>/<unit>' with unit one of "
            f"second, minute, hour, day (e.g. '60/minute')"
        )
    limit = int(m.group(1))
    if limit < 1:
        raise ValueError(f"invalid rate {text!r}: the count must be at least 1")
    return Rate(limit=limit, window=RATE_UNITS[m.group(2)])


def parse_rule_key(key: str) -> tuple[str | None, str]:
    """Split an app.toml key into ``(method or None, path)``.

    >>> parse_rule_key("POST /attempts")
    ('POST', '/attempts')
    >>> parse_rule_key("/search")
    (None, '/search')
    """
    parts = key.split()
    if len(parts) == 1:
        method, path = None, parts[0]
    elif len(parts) == 2:
        method, path = parts[0].upper(), parts[1]
        if method != "*" and method not in _HTTP_METHODS:
            raise ValueError(f"invalid rate_limit key {key!r}: unknown method")
        method = None if method == "*" else method
    else:
        raise ValueError(f"invalid rate_limit key {key!r}: expected '[METHOD] /path'")
    if not path.startswith("/"):
        raise ValueError(f"invalid rate_limit key {key!r}: path must start with /")
    if "%" in path:
        # Paths are matched decoded; an encoded key could never match.
        raise ValueError(
            f"invalid rate_limit key {key!r}: write the path decoded, without %"
        )
    if normalize_path(path) != path:
        raise ValueError(
            f"invalid rate_limit key {key!r}: path must be normalised "
            f"(no //, . or .. segments)"
        )
    return method, path


def validate_rate_limits(table: Mapping[str, str]) -> dict[str, str]:
    """Check an app.toml ``[rate_limit]`` table, raising ValueError on any bad entry."""
    if not isinstance(table, Mapping):
        raise ValueError("[rate_limit] must be a table of '[METHOD] /path' = 'N/unit'")
    for key, rate in table.items():
        parse_rule_key(key)
        parse_rate(rate)
    return dict(table)


def rules_for_app(app: str, route_prefix: str, table: Mapping[str, str]):
    """The app's declared limits as rules on full request paths."""
    prefix = route_prefix.rstrip("/")
    for key, rate in table.items():
        method, path = parse_rule_key(key)
        yield RateLimitRule(
            app=app,
            method=method,
            path=prefix + path,
            rate=parse_rate(rate),
            spec=f"{key} = {rate}",
        )


# --------------------------------------------------------------------- client key


Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(proxies: Iterable[str | Network]) -> list[Network]:
    """Parse IPs/CIDRs (strings or network objects) into networks; raises ValueError.

    Call it once, at startup, so a typo fails loudly there, not on every request.

    >>> parse_networks(["127.0.0.1", "10.0.0.0/8"])
    [IPv4Network('127.0.0.1/32'), IPv4Network('10.0.0.0/8')]
    """
    return [
        p
        if isinstance(p, (ipaddress.IPv4Network, ipaddress.IPv6Network))
        else ipaddress.ip_network(str(p).strip(), strict=False)
        for p in proxies
    ]


def parse_address(text: str | None):
    """An IP address from a peer or forwarded hop, or None if it is not one.

    Strips a port and IPv6 brackets, and unwraps IPv4-mapped IPv6 addresses.

    >>> [str(parse_address(t)) for t in ("1.2.3.4:80", "[::1]:443", "::ffff:10.0.0.1")]
    ['1.2.3.4', '::1', '10.0.0.1']
    >>> parse_address("unknown") is None
    True
    >>> str(parse_address("fe80::1%eth0"))
    'fe80::1'
    """
    if not text:
        return None
    text = text.strip()
    if text.startswith("["):
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:  # IPv4 with a port
        text = text.split(":", 1)[0]
    text = text.split("%", 1)[0]  # an IPv6 zone id is client-chosen noise
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _in_networks(ip, networks: list[Network]) -> bool:
    return ip is not None and any(ip in net for net in networks)


def client_address(
    peer: str | None, forwarded_for: str | None, *, trusted: list[Network]
) -> str:
    """The client address: ``X-Forwarded-For`` only via a trusted peer, right to left.

    >>> nets = parse_networks(["127.0.0.1"])
    >>> client_address("127.0.0.1", "6.6.6.6, 1.2.3.4", trusted=nets)
    '1.2.3.4'
    >>> client_address("5.5.5.5", "1.2.3.4", trusted=nets)  # untrusted peer
    '5.5.5.5'
    """
    peer_ip = parse_address(peer)
    peer = str(peer_ip) if peer_ip is not None else (peer or "unknown")
    if not trusted or not forwarded_for or not _in_networks(peer_ip, trusted):
        return peer
    for hop in reversed([h.strip() for h in forwarded_for.split(",") if h.strip()]):
        hop_ip = parse_address(hop)
        if hop_ip is None:
            # Not an address: the chain cannot be trusted past this point.
            return peer
        if not _in_networks(hop_ip, trusted):
            return str(hop_ip)
    return peer


def bucket_key(address: str, *, ipv6_prefix: int = DEFAULT_IPV6_PREFIX) -> str:
    """IPv4 as is; IPv6 grouped by its /``ipv6_prefix``.

    >>> bucket_key("2001:db8::1") == bucket_key("2001:db8::ffff")
    True
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/{ipv6_prefix}", strict=False))
    return str(ip)


# ------------------------------------------------------------------------ limiter


class Limiter(Protocol):
    """Take one request for ``key``: 0 if allowed, else seconds until it would be."""

    def acquire(self, key: str) -> float: ...


class _AllowMixin:
    """``allow(key) -> bool`` for callers that only need the yes/no of ``acquire``."""

    def allow(self, key: str) -> bool:
        """Take one request for ``key``; True if it is allowed now."""
        return self.acquire(key) <= 0


class TokenBucketLimiter(_AllowMixin):
    """In-memory token buckets, one per key, bounded in number (LRU eviction).

    A bucket holds up to ``rate.limit`` tokens and refills continuously at
    ``limit / window`` per second, so bursts up to the limit pass and the
    sustained rate is the declared one.
    """

    def __init__(
        self,
        rate: Rate,
        *,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_keys < 1:
            raise ValueError(f"max_keys must be at least 1, got {max_keys}")
        self.rate = rate
        self.capacity = float(rate.limit)
        self.refill_per_second = rate.limit / rate.window
        self.max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def acquire(self, key: str) -> float:
        with self._lock:
            now = self._clock()
            tokens, last = self._buckets.pop(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)
            if tokens >= 1:
                wait, tokens = 0.0, tokens - 1
            else:
                wait = (1 - tokens) / self.refill_per_second
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
            return wait


class SlidingWindowLimiter(_AllowMixin):
    """In-memory sliding window: at most ``rate.limit`` requests in any ``window``.

    Unlike :class:`TokenBucketLimiter` there is no refill between hits: a client
    that spent its budget waits until its oldest hit leaves the window. Per process
    (N workers allow N times the rate); the number of tracked keys is bounded
    (``max_keys``, least recently seen evicted) so a flood of distinct keys cannot
    exhaust memory. Each key keeps up to ``rate.limit`` timestamps, so memory is
    bounded by ``max_keys * rate.limit``: prefer :class:`TokenBucketLimiter` (one
    pair of floats per key) for large limits.

    >>> now = [0.0]
    >>> lim = SlidingWindowLimiter(Rate(1, 10), clock=lambda: now[0])
    >>> lim.acquire("a"), lim.acquire("a")
    (0.0, 10.0)
    >>> now[0] = 10.0
    >>> lim.allow("a")
    True
    """

    def __init__(
        self,
        rate: Rate,
        *,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_keys < 1:
            raise ValueError(f"max_keys must be at least 1, got {max_keys}")
        self.rate = rate
        self.max_keys = max_keys
        self._clock = clock
        self._hits: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()  # callers may run in a threadpool

    def acquire(self, key: str) -> float:
        with self._lock:
            now = self._clock()
            hits = self._hits.get(key)
            if hits is None:
                hits = self._hits[key] = deque()
                while len(self._hits) > self.max_keys:
                    self._hits.popitem(last=False)
            else:
                self._hits.move_to_end(key)
            cutoff = now - self.rate.window
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.rate.limit:
                return hits[0] - cutoff  # > 0: when the oldest hit leaves the window
            hits.append(now)
            return 0.0


class RedisLimiter(_AllowMixin):
    """Fixed window in Redis: one count per key per window, shared by every worker.

    ``client`` is any redis-py compatible client (injected; tests use a fake), or
    build one with :meth:`from_url`. When Redis errors, the request is decided by
    ``fallback`` (a per-process in-memory limiter by default), so an outage
    degrades protection instead of either refusing everything or lifting the limit.

    A fixed window admits up to twice ``rate.limit`` across a window boundary; the
    price of one round trip and one counter per key. Keys go into Redis as given,
    so they must come from a bounded set (client bucket keys, rule ids), never raw
    client input. After an error the fallback alone decides for
    ``retry_seconds`` (one warning per outage), so a hung Redis costs one
    ``socket_timeout`` per interval rather than one per request. ``max_keys`` sizes
    the default fallback only.

    ``acquire`` does blocking I/O: :class:`RateLimitMiddleware` runs a limiter whose
    ``blocking`` attribute is true in a worker thread, off the event loop.
    """

    blocking = True

    #: Shown when the optional ``redis`` client is not installed.
    install_hint = "pip install 'enlace[redis]'"

    def __init__(
        self,
        client: Any,
        rate: Rate,
        *,
        namespace: str = "enlace:rl",
        fallback: Limiter | None = None,
        max_keys: int = DEFAULT_MAX_KEYS,
        retry_seconds: float = DEFAULT_REDIS_RETRY_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        self.client = client
        self.rate = rate
        self.namespace = namespace
        self.fallback = fallback or TokenBucketLimiter(rate, max_keys=max_keys)
        self.retry_seconds = retry_seconds
        self._clock = clock
        self._down_until: float | None = None  # set while Redis is failing

    @classmethod
    def from_url(
        cls,
        url: str,
        *args,
        socket_timeout: float = DEFAULT_REDIS_SOCKET_TIMEOUT,
        **kwargs,
    ):
        """Build from a ``redis://`` URL (needs the ``redis`` client installed).

        ``socket_timeout`` bounds both connect and reads, so a hung Redis costs at
        most that long before the fallback limiter decides. Other arguments go to
        the constructor.
        """
        try:
            import redis  # lazy: optional dependency
        except ImportError as e:  # pragma: no cover - depends on environment
            raise ImportError(
                f"The Redis rate limiter needs the redis client: {cls.install_hint}"
            ) from e
        client = redis.Redis.from_url(
            url, socket_timeout=socket_timeout, socket_connect_timeout=socket_timeout
        )
        return cls(client, *args, **kwargs)

    def redis_key(self, key: str, index: int) -> str:
        """The Redis counter for ``key`` in window number ``index``.

        The window length is part of it, so two limiters sharing a namespace but
        not a rate never share a counter.
        """
        return f"{self.namespace}:{self.rate.window:g}:{key}:{index}"

    def acquire(self, key: str) -> float:
        window = self.rate.window
        now = self._clock()
        down_until = self._down_until
        if down_until is not None and now < down_until:
            return self.fallback.acquire(key)
        index = int(now // window)
        redis_key = self.redis_key(key, index)
        try:
            pipe = self.client.pipeline()
            pipe.incr(redis_key)
            pipe.expire(redis_key, math.ceil(window) + 1)
            count = pipe.execute()[0]
        except Exception:  # noqa: BLE001 - any client/network failure
            if down_until is None:
                logger.warning(
                    "Redis rate limiter unavailable; using the fallback limiter "
                    "for %ss at a time",
                    self.retry_seconds,
                    exc_info=True,
                )
            self._down_until = now + self.retry_seconds
            return self.fallback.acquire(key)
        if down_until is not None:
            logger.info("Redis rate limiter reachable again")
            self._down_until = None
        if int(count) <= self.rate.limit:
            return 0.0
        wait = (index + 1) * window - now  # until the next window opens
        return wait if wait > 0 else window  # guard float rounding at a boundary


# --------------------------------------------------------------------- middleware


class RateLimitMiddleware:
    """Pure ASGI middleware enforcing :class:`RateLimitRule` s per client.

    Every rule a request matches is charged (a request may count against a
    narrow and a broad rule); the first one exhausted answers 429.
    """

    def __init__(
        self,
        app,
        *,
        rules: Iterable[RateLimitRule],
        trusted_proxies: Iterable[str | Network] = (),
        make_limiter: Callable[[Rate], Limiter] = TokenBucketLimiter,
        ipv6_prefix: int = DEFAULT_IPV6_PREFIX,
    ):
        self.app = app
        self.rules = [(rule, make_limiter(rule.rate)) for rule in rules]
        self.trusted = parse_networks(trusted_proxies)
        if not 0 <= ipv6_prefix <= 128:
            raise ValueError(f"ipv6_prefix must be within 0..128, got {ipv6_prefix}")
        self.ipv6_prefix = ipv6_prefix

    def _client_key(self, scope) -> str:
        client = scope.get("client")
        peer = client[0] if client else None
        forwarded = None
        for name, value in scope.get("headers", ()):
            if name == b"x-forwarded-for":
                # Several headers are one comma-joined list (RFC 9110 §5.3).
                text = value.decode("latin-1")
                forwarded = text if forwarded is None else f"{forwarded}, {text}"
        address = client_address(peer, forwarded, trusted=self.trusted)
        return bucket_key(address, ipv6_prefix=self.ipv6_prefix)

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket") or not self.rules:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET") if kind == "http" else "GET"
        if method == "OPTIONS" and _is_preflight(scope):
            await self.app(scope, receive, send)
            return
        path = normalize_path(scope["path"])
        matched = [(r, lim) for r, lim in self.rules if r.matches(method, path)]
        if matched:
            client = self._client_key(scope)
            for rule, limiter in matched:
                # The rule's id keeps two rules apart in a limiter they share.
                key = f"{rule.id}|{client}"
                if getattr(limiter, "blocking", False):
                    from anyio.to_thread import run_sync  # starlette's own dep

                    wait = await run_sync(limiter.acquire, key)
                else:
                    wait = limiter.acquire(key)
                if wait > 0:
                    if kind == "websocket":
                        await send({"type": "websocket.close", "code": 1008})
                    else:
                        await _too_many_requests(send, wait, rule)
                    return
        await self.app(scope, receive, send)


def _is_preflight(scope) -> bool:
    return any(
        name == b"access-control-request-method" for name, _ in scope.get("headers", ())
    )


async def _too_many_requests(send, wait: float, rule: RateLimitRule) -> None:
    body = json.dumps(
        {"detail": "Too Many Requests", "limit": rule.spec, "app": rule.app}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(max(1, math.ceil(wait))).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def add_rate_limits(parent, config) -> bool:
    """Install :class:`RateLimitMiddleware` on *parent* if any app declares limits."""
    rules = [
        rule
        for app in config.apps
        if app.rate_limit
        for rule in rules_for_app(app.name, app.route_prefix, app.rate_limit)
    ]
    if not rules:
        return False
    parent.add_middleware(
        RateLimitMiddleware, rules=rules, trusted_proxies=config.trusted_proxies
    )
    return True
