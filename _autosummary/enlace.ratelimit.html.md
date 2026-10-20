# enlace.ratelimit

Opt-in, per-app, per-client rate limits for mounted apps.

An app declares limits in its `app.toml`; enlace enforces them in front of the
app, so the app needs no code (and still runs alone without them):

```default
[rate_limit]
"POST /attempts" = "60/minute"      # method + path under the app's API prefix
"/search" = "10/second"             # no method: any method
"POST /items/*" = "100/hour"        # fnmatch-style glob
```

A request over its limit gets `429 Too Many Requests` with `Retry-After`.

**Who the client is.** Behind a reverse proxy the TCP peer is the proxy, so the
client address comes from `X-Forwarded-For` – but only when the peer is one of
platform.toml’s `[platform] trusted_proxies`, and read right to left skipping
trusted hops, so a client cannot spoof it by sending the header itself. With no
`trusted_proxies` the peer address in the ASGI scope is used as is (which is
already the client when the server itself resolves proxy headers, as uvicorn does
for a loopback proxy by default). IPv6 clients share a bucket per /64.

**What is matched.** The request path is normalised first (`//`, `.` and
`..` collapsed, as an upstream would resolve them), so a rule cannot be
sidestepped with `/a/../attempts`. A `GET` rule also covers `HEAD`. CORS
preflights (`OPTIONS` with `Access-Control-Request-Method`) are never counted.
A websocket handshake is matched as `GET` and refused with close code 1008.

**Scope of the guarantee.** Buckets are in memory, per process: a host running N
worker processes allows up to N times the declared rate. Each rule tracks at most
`max_keys` clients and evicts the least recently seen, and an evicted bucket
comes back full, so a client rotating through more addresses than that (e.g. many
IPv6 /64s) is not held to the rate. It bounds abuse; it is not an exact quota. The
limiter is a seam (`make_limiter`) for a shared one.

\*\*In production behind a proxy, set 

```
``
```

trusted_proxies\`\`\*\* unless the server
already resolves the client from the proxy’s header: otherwise every client
shares the proxy’s address, and one abusive client exhausts everyone’s bucket.

### Module Attributes

| [`RATE_UNITS`](#enlace.ratelimit.RATE_UNITS)          | Window lengths, in seconds, a rate may be expressed in ("60/minute").                                                                               |
|----------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------|
| [`DEFAULT_MAX_KEYS`](#enlace.ratelimit.DEFAULT_MAX_KEYS)    | Client buckets one rule tracks before evicting the least recently seen, so a flood of distinct (spoofed or rotating) clients cannot exhaust memory. |
| [`DEFAULT_IPV6_PREFIX`](#enlace.ratelimit.DEFAULT_IPV6_PREFIX) | one host usually owns a whole /64.                                                                                                                  |

### Functions

| [`add_rate_limits`](#enlace.ratelimit.add_rate_limits)(parent, config)                  | Install [`RateLimitMiddleware`](#enlace.ratelimit.RateLimitMiddleware) on *parent* if any app declares limits.   |
|---------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------|
| [`bucket_key`](#enlace.ratelimit.bucket_key)(address, \*[, ipv6_prefix])           | IPv4 as is; IPv6 grouped by its /`ipv6_prefix`.                                                                        |
| [`client_address`](#enlace.ratelimit.client_address)(peer, forwarded_for, \*, trusted) | The client address: `X-Forwarded-For` only via a trusted peer, right to left.                                          |
| [`normalize_path`](#enlace.ratelimit.normalize_path)(path)                             | Collapse `//`, `.` and `..` the way an upstream would resolve them.                                                    |
| [`parse_address`](#enlace.ratelimit.parse_address)(text)                              | An IP address from a peer or forwarded hop, or None if it is not one.                                                  |
| [`parse_networks`](#enlace.ratelimit.parse_networks)(proxies)                          | Parse IPs/CIDRs into networks; raises ValueError (call it once, at startup).                                           |
| [`parse_rate`](#enlace.ratelimit.parse_rate)(text)                                 | Parse `"60/minute"` (also `60/min`, `60/m`, `5/seconds`...).                                                           |
| [`parse_rule_key`](#enlace.ratelimit.parse_rule_key)(key)                              | Split an app.toml key into `(method or None, path)`.                                                                   |
| [`rules_for_app`](#enlace.ratelimit.rules_for_app)(app, route_prefix, table)          | The app's declared limits as rules on full request paths.                                                              |
| [`validate_rate_limits`](#enlace.ratelimit.validate_rate_limits)(table)                      | Check an app.toml `[rate_limit]` table, raising ValueError on any bad entry.                                           |

### Classes

| [`Limiter`](#enlace.ratelimit.Limiter)(\*args, \*\*kwargs)                     | Take one request for `key`: 0 if allowed, else seconds until it would be.                                   |
|--------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------|
| [`Rate`](#enlace.ratelimit.Rate)(limit, window)                             | `limit` requests per `window` seconds.                                                                      |
| [`RateLimitMiddleware`](#enlace.ratelimit.RateLimitMiddleware)(app, \*, rules[, ...])      | Pure ASGI middleware enforcing [`RateLimitRule`](#enlace.ratelimit.RateLimitRule) s per client. |
| [`RateLimitRule`](#enlace.ratelimit.RateLimitRule)(app, method, path, rate, spec)    | One declared limit: requests matching `method` + `path` share `rate`.                                       |
| [`TokenBucketLimiter`](#enlace.ratelimit.TokenBucketLimiter)(rate, \*[, max_keys, clock]) | In-memory token buckets, one per key, bounded in number (LRU eviction).                                     |

### enlace.ratelimit.DEFAULT_IPV6_PREFIX *= 64*

one host usually owns a whole /64.

* **Type:**
  IPv6 clients are grouped by this prefix

### enlace.ratelimit.DEFAULT_MAX_KEYS *= 100000*

Client buckets one rule tracks before evicting the least recently seen, so a
flood of distinct (spoofed or rotating) clients cannot exhaust memory.

### *class* enlace.ratelimit.Limiter(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Take one request for `key`: 0 if allowed, else seconds until it would be.

### enlace.ratelimit.RATE_UNITS *= {'d': 86400, 'day': 86400, 'h': 3600, 'hour': 3600, 'm': 60, 'min': 60, 'minute': 60, 's': 1, 'sec': 1, 'second': 1}*

Window lengths, in seconds, a rate may be expressed in (“60/minute”).

### *class* enlace.ratelimit.Rate(limit, window)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

`limit` requests per `window` seconds.

### *class* enlace.ratelimit.RateLimitMiddleware(app, \*, rules, trusted_proxies=(), make_limiter=<class 'enlace.ratelimit.TokenBucketLimiter'>, ipv6_prefix=64)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Pure ASGI middleware enforcing [`RateLimitRule`](#enlace.ratelimit.RateLimitRule) s per client.

Every rule a request matches is charged (a request may count against a
narrow and a broad rule); the first one exhausted answers 429.

### *class* enlace.ratelimit.RateLimitRule(app, method, path, rate, spec)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One declared limit: requests matching `method` + `path` share `rate`.

`method` is `None` for any method; `path` is a full request path (or an
fnmatch glob) and `spec` is the app.toml text it came from, for messages.

#### matches(method, path)

Whether a request matches; *path* should be [`normalize_path()`](#enlace.ratelimit.normalize_path) ed.

```pycon
>>> r = RateLimitRule("a", "GET", "/x", Rate(1, 1), "")
>>> r.matches("HEAD", "/x"), r.matches("POST", "/x")
(True, False)
```

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### *class* enlace.ratelimit.TokenBucketLimiter(rate, \*, max_keys=100000, clock=<built-in function monotonic>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

In-memory token buckets, one per key, bounded in number (LRU eviction).

A bucket holds up to `rate.limit` tokens and refills continuously at
`limit / window` per second, so bursts up to the limit pass and the
sustained rate is the declared one.

### enlace.ratelimit.add_rate_limits(parent, config)

Install [`RateLimitMiddleware`](#enlace.ratelimit.RateLimitMiddleware) on *parent* if any app declares limits.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### enlace.ratelimit.bucket_key(address, , ipv6_prefix=64)

IPv4 as is; IPv6 grouped by its /`ipv6_prefix`.

```pycon
>>> bucket_key("2001:db8::1") == bucket_key("2001:db8::ffff")
True
```

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### enlace.ratelimit.client_address(peer, forwarded_for, , trusted)

The client address: `X-Forwarded-For` only via a trusted peer, right to left.

```pycon
>>> nets = parse_networks(["127.0.0.1"])
>>> client_address("127.0.0.1", "6.6.6.6, 1.2.3.4", trusted=nets)
'1.2.3.4'
>>> client_address("5.5.5.5", "1.2.3.4", trusted=nets)  # untrusted peer
'5.5.5.5'
```

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### enlace.ratelimit.normalize_path(path)

Collapse `//`, `.` and `..` the way an upstream would resolve them.

```pycon
>>> normalize_path("/api/x/foo/../attempts"), normalize_path("//api/./x//a/")
('/api/x/attempts', '/api/x/a/')
```

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### enlace.ratelimit.parse_address(text)

An IP address from a peer or forwarded hop, or None if it is not one.

Strips a port and IPv6 brackets, and unwraps IPv4-mapped IPv6 addresses.

```pycon
>>> [str(parse_address(t)) for t in ("1.2.3.4:80", "[::1]:443", "::ffff:10.0.0.1")]
['1.2.3.4', '::1', '10.0.0.1']
>>> parse_address("unknown") is None
True
```

### enlace.ratelimit.parse_networks(proxies)

Parse IPs/CIDRs into networks; raises ValueError (call it once, at startup).

```pycon
>>> parse_networks(["127.0.0.1", "10.0.0.0/8"])
[IPv4Network('127.0.0.1/32'), IPv4Network('10.0.0.0/8')]
```

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`IPv4Network`](https://docs.python.org/3/library/ipaddress.html#ipaddress.IPv4Network) | [`IPv6Network`](https://docs.python.org/3/library/ipaddress.html#ipaddress.IPv6Network)]

### enlace.ratelimit.parse_rate(text)

Parse `"60/minute"` (also `60/min`, `60/m`, `5/seconds`…).

```pycon
>>> parse_rate("60/minute")
Rate(limit=60, window=60)
>>> parse_rate("10 / s")
Rate(limit=10, window=1)
```

* **Return type:**
  [`Rate`](#enlace.ratelimit.Rate)

### enlace.ratelimit.parse_rule_key(key)

Split an app.toml key into `(method or None, path)`.

```pycon
>>> parse_rule_key("POST /attempts")
('POST', '/attempts')
>>> parse_rule_key("/search")
(None, '/search')
```

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str) | [`None`](https://docs.python.org/3/builtins/constants.html#None), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### enlace.ratelimit.rules_for_app(app, route_prefix, table)

The app’s declared limits as rules on full request paths.

### enlace.ratelimit.validate_rate_limits(table)

Check an app.toml `[rate_limit]` table, raising ValueError on any bad entry.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]
