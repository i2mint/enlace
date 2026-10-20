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

**Scope of the guarantee.** Buckets are in memory, per process: a host running N
worker processes allows up to N times the declared rate. That bounds abuse; it is
not an exact quota. The limiter is a seam (``make_limiter``) for a shared one.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

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

_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*([a-z]+?)s?\s*$")
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


@dataclass(frozen=True)
class Rate:
    """``limit`` requests per ``window`` seconds."""

    limit: int
    window: float


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
        if self.method is not None and method != self.method:
            return False
        return fnmatch.fnmatchcase(path, self.path)


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


def parse_networks(proxies: Iterable[str]) -> list[Network]:
    """Parse IPs/CIDRs into networks; raises ValueError (call it once, at startup).

    >>> parse_networks(["127.0.0.1", "10.0.0.0/8"])
    [IPv4Network('127.0.0.1/32'), IPv4Network('10.0.0.0/8')]
    """
    return [ipaddress.ip_network(str(p).strip(), strict=False) for p in proxies]


def _in_networks(addr: str, networks: list[Network]) -> bool:
    try:
        ip = ipaddress.ip_address(addr.strip())
    except ValueError:
        return False
    return any(ip in net for net in networks)


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
    peer = peer or "unknown"
    if not trusted or not forwarded_for or not _in_networks(peer, trusted):
        return peer
    for hop in reversed([h.strip() for h in forwarded_for.split(",") if h.strip()]):
        if not _in_networks(hop, trusted):
            return hop
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


class TokenBucketLimiter:
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
        trusted_proxies: Iterable[str] = (),
        make_limiter: Callable[[Rate], Limiter] = TokenBucketLimiter,
        ipv6_prefix: int = DEFAULT_IPV6_PREFIX,
    ):
        self.app = app
        self.rules = [(rule, make_limiter(rule.rate)) for rule in rules]
        self.trusted = parse_networks(trusted_proxies)
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
        if scope["type"] != "http" or not self.rules:
            await self.app(scope, receive, send)
            return
        method, path = scope["method"], scope["path"]
        matched = [(r, lim) for r, lim in self.rules if r.matches(method, path)]
        if matched:
            key = self._client_key(scope)
            for rule, limiter in matched:
                wait = limiter.acquire(key)
                if wait > 0:
                    await _too_many_requests(send, wait, rule)
                    return
        await self.app(scope, receive, send)


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
