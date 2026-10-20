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
for a loopback proxy by default). IPv6 clients share a bucket per /64. Behind a
proxy on a unix socket (``uvicorn --uds``) there is no peer address at all: pass
``trust_unix_socket=True`` (platform.toml ``[platform] trust_unix_socket = true``)
so the proxy's header is read; otherwise every client shares one "unknown" bucket.

**What is matched.** The request path is normalised first (``//``, ``.`` and
``..`` collapsed, as an upstream would resolve them), so a rule cannot be
sidestepped with ``/a/../attempts``. A ``GET`` rule also covers ``HEAD``. CORS
preflights (``OPTIONS`` with ``Access-Control-Request-Method``) are never counted.
A websocket handshake is matched as ``GET`` and refused with close code 1008.

**Scope of the guarantee.** Buckets are in memory, per process: a host running N
worker processes allows up to N times the declared rate. Each rule tracks at most
``max_keys`` clients. When full, idle buckets (full again, or with no hit left in
the window) are dropped first, since they carry no information; only then is the
least recently seen evicted (``on_overflow="evict"``, the default, warned once),
and an evicted bucket comes back full, so a client rotating through more addresses
than that (e.g. many IPv6 /64s) is not held to the rate. ``on_overflow="deny"``
refuses new clients instead (fail closed). It bounds abuse; it is not an exact
quota. The limiter is a seam (``make_limiter``) for a shared one.

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
import inspect
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
#: What an in-memory limiter does with a new key when every tracked one is busy:
#: ``"evict"`` the least recently seen (fail open), or ``"deny"`` the new key.
OVERFLOW_POLICIES = ("evict", "deny")
#: A full in-memory limiter looks for idle keys at most this often, so a flood of
#: new keys cannot make every request pay for a sweep.
DEFAULT_MIN_SWEEP_SECONDS = 1.0
#: Keys one sweep examines: a sweep of a full limiter is a slice of its keys
#: (oldest first) rather than all ``max_keys``, so it cannot stall the event loop.
DEFAULT_SWEEP_BATCH = 1024
#: What the Redis limiter does while Redis is failing: ask its ``fallback``, or
#: refuse (``"deny"``, fail closed) or admit (``"allow"``, fail open) every request.
REDIS_ERROR_POLICIES = ("fallback", "deny", "allow")
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
    peer: str | None,
    forwarded_for: str | None,
    *,
    trusted: list[Network],
    trust_unix_socket: bool = False,
) -> str:
    """The client address: ``X-Forwarded-For`` only via a trusted peer, right to left.

    A missing peer (``None``: a unix-socket connection) is trusted only with
    ``trust_unix_socket``, for a proxy that reaches the server over a socket.

    >>> nets = parse_networks(["127.0.0.1"])
    >>> client_address("127.0.0.1", "6.6.6.6, 1.2.3.4", trusted=nets)
    '1.2.3.4'
    >>> client_address("5.5.5.5", "1.2.3.4", trusted=nets)  # untrusted peer
    '5.5.5.5'
    >>> client_address(None, "1.2.3.4", trusted=[]), client_address(
    ...     None, "1.2.3.4", trusted=[], trust_unix_socket=True)
    ('unknown', '1.2.3.4')
    """
    peer_ip = parse_address(peer)
    peer_trusted = _in_networks(peer_ip, trusted) or (not peer and trust_unix_socket)
    peer = str(peer_ip) if peer_ip is not None else (peer or "unknown")
    if not forwarded_for or not peer_trusted:
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


class _BoundedKeys:
    """The key bound of the in-memory limiters: idle keys go first, then the policy.

    A subclass keeps its per-key state in an ``OrderedDict`` (least recently seen
    first) and says, with :meth:`_idle_in`, how long until a key's state carries no
    information (a full bucket, an empty window).

    Sweeps are incremental: a round snapshots the keys (least recently seen first,
    the likeliest to be idle) and each sweep examines the next ``sweep_batch`` of
    them, so the work done under the lock is bounded however large ``max_keys`` is.
    """

    def _init_bound(
        self,
        max_keys: int,
        on_overflow: str,
        min_sweep_seconds: float,
        sweep_batch: int = DEFAULT_SWEEP_BATCH,
    ):
        if max_keys < 1:
            raise ValueError(f"max_keys must be at least 1, got {max_keys}")
        if sweep_batch < 1:
            raise ValueError(f"sweep_batch must be at least 1, got {sweep_batch}")
        if on_overflow not in OVERFLOW_POLICIES:
            raise ValueError(
                f"on_overflow must be one of {OVERFLOW_POLICIES}, got {on_overflow!r}"
            )
        self.max_keys = max_keys
        self.on_overflow = on_overflow
        self.min_sweep_seconds = min_sweep_seconds
        self.sweep_batch = sweep_batch
        self._next_sweep = -math.inf
        self._sweep_queue: deque = deque()  # keys left in the current round
        self._round_soonest = math.inf  # when a key examined this round goes idle
        self._admitted_soonest = math.inf  # ... or a key added since it began
        self._warned_eviction = False

    def _idle_in(self, entry, now: float) -> float:  # pragma: no cover - abstract
        """Seconds until *entry* is idle (``<= 0``: idle now)."""
        raise NotImplementedError

    def _make_room(self, entries: OrderedDict, now: float) -> float:
        """Room for one new key: 0 if it may be added, else the seconds to refuse it.

        Drops idle keys (one sweep at most every ``min_sweep_seconds``, and none
        before the first key could have gone idle). With no idle key, ``"evict"``
        admits it (the caller trims the least recently seen) and ``"deny"`` refuses.
        """
        if len(entries) < self.max_keys:
            return 0.0
        if now >= self._next_sweep:
            self._sweep(entries, now)
            if len(entries) < self.max_keys:
                return 0.0
        if self.on_overflow == "deny":
            return self._next_sweep - now  # > 0: when a key may next go idle
        return 0.0

    def _sweep(self, entries: OrderedDict, now: float) -> None:
        """Drop the idle keys among the next ``sweep_batch`` of the round.

        A key examined earlier in the round may have been seen again since: its
        remembered idle time is then too early, which only brings a sweep forward.
        """
        if not self._sweep_queue:
            self._sweep_queue.extend(entries)
            self._round_soonest = self._admitted_soonest = math.inf
        queue = self._sweep_queue
        for _ in range(min(self.sweep_batch, len(queue))):
            key = queue.popleft()
            entry = entries.get(key)
            if entry is None:
                continue  # gone since the round began
            idle_in = self._idle_in(entry, now)
            if idle_in <= 0:
                del entries[key]
            else:
                self._round_soonest = min(self._round_soonest, now + idle_in)
        if queue:  # keys not examined yet may be idle already
            self._next_sweep = now + self.min_sweep_seconds
        else:
            soonest = min(self._round_soonest, self._admitted_soonest)
            self._next_sweep = max(soonest, now + self.min_sweep_seconds)

    def _admitted(self, entry, now: float) -> None:
        """A new key was stored: it may go idle before the remembered sweep horizon.

        Remembered for the round too: the round's snapshot does not hold the key, so
        the horizon set when the round ends must not overlook it.
        """
        idle_at = now + self._idle_in(entry, now)
        self._next_sweep = min(self._next_sweep, idle_at)
        self._admitted_soonest = min(self._admitted_soonest, idle_at)

    def _trim(self, entries: OrderedDict) -> None:
        """Evict the least recently seen keys beyond ``max_keys`` (warned once)."""
        if len(entries) <= self.max_keys:
            return
        if not self._warned_eviction:
            self._warned_eviction = True
            logger.warning(
                "%s full (%d keys, none idle): evicting the least recently seen, "
                "whose limits restart. Raise max_keys, or pass on_overflow='deny' "
                "to refuse new clients instead.",
                type(self).__name__,
                self.max_keys,
            )
        while len(entries) > self.max_keys:
            entries.popitem(last=False)


class TokenBucketLimiter(_BoundedKeys, _AllowMixin):
    """In-memory token buckets, one per key, bounded in number (LRU eviction).

    A bucket holds up to ``rate.limit`` tokens and refills continuously at
    ``limit / window`` per second, so bursts up to the limit pass and the
    sustained rate is the declared one. At ``max_keys`` keys, full buckets are
    dropped first, then ``on_overflow`` decides (see :class:`_BoundedKeys`).

    >>> now = [0.0]
    >>> lim = TokenBucketLimiter(Rate(1, 10), max_keys=1, on_overflow="deny",
    ...     clock=lambda: now[0])
    >>> lim.acquire("a"), lim.acquire("b")  # "a" is busy: the new "b" is refused
    (0.0, 10.0)
    >>> now[0] = 10.0  # "a" is full again: idle, so it makes room
    >>> lim.allow("b")
    True
    """

    def __init__(
        self,
        rate: Rate,
        *,
        max_keys: int = DEFAULT_MAX_KEYS,
        on_overflow: str = "evict",
        min_sweep_seconds: float = DEFAULT_MIN_SWEEP_SECONDS,
        sweep_batch: int = DEFAULT_SWEEP_BATCH,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._init_bound(max_keys, on_overflow, min_sweep_seconds, sweep_batch)
        self.rate = rate
        self.capacity = float(rate.limit)
        self.refill_per_second = rate.limit / rate.window
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _idle_in(self, entry, now: float) -> float:
        tokens, last = entry
        return (self.capacity - tokens) / self.refill_per_second - (now - last)

    def acquire(self, key: str) -> float:
        with self._lock:
            now = self._clock()
            new = key not in self._buckets
            if new:
                refused = self._make_room(self._buckets, now)
                if refused > 0:
                    return refused
            tokens, last = self._buckets.pop(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)
            if tokens >= 1:
                wait, tokens = 0.0, tokens - 1
            else:
                wait = (1 - tokens) / self.refill_per_second
            self._buckets[key] = (tokens, now)
            if new:
                self._admitted((tokens, now), now)
            self._trim(self._buckets)
            return wait


class SlidingWindowLimiter(_BoundedKeys, _AllowMixin):
    """In-memory sliding window: at most ``rate.limit`` requests in any ``window``.

    Unlike :class:`TokenBucketLimiter` there is no refill between hits: a client
    that spent its budget waits until its oldest hit leaves the window. Per process
    (N workers allow N times the rate); the number of tracked keys is bounded
    (``max_keys``: keys with no hit left in the window go first, then
    ``on_overflow`` decides) so a flood of distinct keys cannot exhaust memory.
    Each key keeps up to ``rate.limit`` timestamps, so memory is bounded by
    ``max_keys * rate.limit``: prefer :class:`TokenBucketLimiter` (one pair of
    floats per key) for large limits.

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
        on_overflow: str = "evict",
        min_sweep_seconds: float = DEFAULT_MIN_SWEEP_SECONDS,
        sweep_batch: int = DEFAULT_SWEEP_BATCH,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._init_bound(max_keys, on_overflow, min_sweep_seconds, sweep_batch)
        self.rate = rate
        self._clock = clock
        self._hits: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()  # callers may run in a threadpool

    def _idle_in(self, entry, now: float) -> float:
        return entry[-1] + self.rate.window - now if entry else 0.0

    def acquire(self, key: str) -> float:
        with self._lock:
            now = self._clock()
            hits = self._hits.get(key)
            new = hits is None
            if new:
                refused = self._make_room(self._hits, now)
                if refused > 0:
                    return refused
                hits = self._hits[key] = deque()
                self._trim(self._hits)
            else:
                self._hits.move_to_end(key)
            cutoff = now - self.rate.window
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.rate.limit:
                return hits[0] - cutoff  # > 0: when the oldest hit leaves the window
            hits.append(now)
            if new:
                self._admitted(hits, now)
            return 0.0


class RedisLimiter(_AllowMixin):
    """Fixed window in Redis: one count per key per window, shared by every worker.

    ``client`` is any redis-py compatible client (injected; tests use a fake), or
    build one with :meth:`from_url`; an asyncio client is refused up front (the
    limiter's calls are synchronous). When Redis errors, ``on_error`` decides:
    ``"fallback"`` (the default) asks ``fallback`` (a per-process in-memory limiter
    by default), so an outage degrades protection instead of either refusing
    everything or lifting the limit; ``"deny"`` refuses every request (fail closed,
    for a public endpoint that would rather refuse than allow N workers times the
    rate) and ``"allow"`` admits every one (fail open).

    A fixed window admits up to twice ``rate.limit`` across a window boundary; the
    price of one round trip and one counter per key. Keys go into Redis as given,
    so they must come from a bounded set (client bucket keys, rule ids), never raw
    client input. After an error ``on_error`` alone decides for
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
        on_error: str = "fallback",
        clock: Callable[[], float] = time.time,
    ):
        if _is_async_redis_client(client):
            raise TypeError(
                f"{type(self).__name__} needs a synchronous redis client "
                f"(redis.Redis), got the asyncio {type(client).__module__}."
                f"{type(client).__name__}: its calls would never be awaited, so "
                f"every request would silently use the fallback."
            )
        if on_error not in REDIS_ERROR_POLICIES:
            raise ValueError(
                f"on_error must be one of {REDIS_ERROR_POLICIES}, got {on_error!r}"
            )
        self.on_error = on_error
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
            return self._while_down(key, now)
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
                    "Redis rate limiter unavailable; %s for %ss at a time",
                    _ON_ERROR_WORDING[self.on_error],
                    self.retry_seconds,
                    exc_info=True,
                )
            self._down_until = now + self.retry_seconds
            return self._while_down(key, now)
        if down_until is not None:
            logger.info("Redis rate limiter reachable again")
            self._down_until = None
        if int(count) <= self.rate.limit:
            return 0.0
        wait = (index + 1) * window - now  # until the next window opens
        return wait if wait > 0 else window  # guard float rounding at a boundary

    def _while_down(self, key: str, now: float) -> float:
        """Decide a request while Redis is failing, per ``on_error``."""
        if self.on_error == "fallback":
            return self.fallback.acquire(key)
        if self.on_error == "allow":
            return 0.0
        wait = self._down_until - now  # "deny": until Redis is tried again
        return wait if wait > 0 else self.rate.window


#: How the outage warning says what :class:`RedisLimiter` does meanwhile.
_ON_ERROR_WORDING = {
    "fallback": "using the fallback limiter",
    "deny": "refusing every request",
    "allow": "admitting every request",
}


def _is_async_redis_client(client: Any) -> bool:
    """Whether *client* is an asyncio redis client (``redis.asyncio.Redis`` and kin)."""
    module = type(client).__module__ or ""
    return module.startswith("redis.asyncio") or inspect.iscoroutinefunction(
        getattr(client, "execute_command", None)
    )


# --------------------------------------------------------------------- middleware


class RateLimitMiddleware:
    """Pure ASGI middleware enforcing :class:`RateLimitRule` s per client.

    Every rule a request matches is charged (a request may count against a
    narrow and a broad rule); the first one exhausted answers 429.

    ``trust_unix_socket`` trusts a connection with no peer address (a proxy on a
    unix socket) to name the client in ``X-Forwarded-For``; anything that can
    reach the socket can then pick its own address, so only the proxy should.
    """

    def __init__(
        self,
        app,
        *,
        rules: Iterable[RateLimitRule],
        trusted_proxies: Iterable[str | Network] = (),
        make_limiter: Callable[[Rate], Limiter] = TokenBucketLimiter,
        ipv6_prefix: int = DEFAULT_IPV6_PREFIX,
        trust_unix_socket: bool = False,
    ):
        self.app = app
        self.rules = [(rule, make_limiter(rule.rate)) for rule in rules]
        self.trusted = parse_networks(trusted_proxies)
        self.trust_unix_socket = trust_unix_socket
        self._warned_no_peer = False
        if trust_unix_socket and self.rules:
            logger.warning(
                "Rate limits trust X-Forwarded-For on connections with no peer "
                "address (unix sockets): make sure only the proxy can reach the socket."
            )
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
        if peer is None and not self.trust_unix_socket and not self._warned_no_peer:
            self._warned_no_peer = True
            logger.warning(
                "Rate-limited request with no peer address (a unix socket?): every "
                "such client shares one 'unknown' bucket. Behind a proxy on a unix "
                "socket, set [platform] trust_unix_socket = true."
            )
        address = client_address(
            peer,
            forwarded,
            trusted=self.trusted,
            trust_unix_socket=self.trust_unix_socket,
        )
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
        RateLimitMiddleware,
        rules=rules,
        trusted_proxies=config.trusted_proxies,
        trust_unix_socket=config.trust_unix_socket,
    )
    return True
