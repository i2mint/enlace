# enlace.response_headers

Per-app response headers declared in `app.toml`; cross-origin isolation in a line.

An app names headers for sub-paths of itself, and enlace adds them to every response
on those paths, whoever produced it (the app’s frontend, its API, the sign-in gate):

```default
cross_origin_isolated = ["lab"]   # COOP + COEP + CORP on /{name}/lab/...

[response_headers]
"lab" = { "Cross-Origin-Embedder-Policy" = "credentialless" }  # overrides one
"/" = { "X-Robots-Tag" = "noindex" }                           # the whole app
```

Both are off by default: an app that declares neither gets no header from here, and
the middleware is not even installed when no app declares one.

**Paths.** A key is a sub-path of the app, `"/"` (or `""`) for all of it. It
applies under each of the app’s mounts: the page at `/{name}/` and the API at its
`route_prefix` (so `CORP: same-origin` also lands on `{route_prefix}/lab/`,
harmless for same-origin fetches). Matching is by whole segments: `"lab"` covers
`/{name}/lab` and `/{name}/lab/...`, never `/{name}/labels`. A request belongs
to the app with the longest mount covering it, and only that app’s keys apply, so
one app’s declaration never reaches another app’s paths. When several of its keys
match, the longest wins header by header. A declared header replaces one of the same
name already on the response. Headers that would corrupt the response or act on the
whole origin (`Set-Cookie`, framing headers, `Clear-Site-Data`,
`Strict-Transport-Security`…) are refused: see [`FORBIDDEN_HEADERS`](#enlace.response_headers.FORBIDDEN_HEADERS).

**Cross-origin isolation.** `cross_origin_isolated = true` (the whole app) or a
list of sub-paths sends [`CROSS_ORIGIN_ISOLATION_HEADERS`](#enlace.response_headers.CROSS_ORIGIN_ISOLATION_HEADERS). A page served with
them gets `crossOriginIsolated === true`, hence `SharedArrayBuffer` (threaded
WebAssembly, memory shared with an `AudioWorklet`) and precise timers. The price,
on every isolated page: a cross-origin resource loads only if it opts in (CORS, or
`Cross-Origin-Resource-Policy: cross-origin`), so third-party embeds (YouTube and
other iframes, analytics and ad scripts, images and fonts from a CDN that sends no
CORP) stop loading; a window it opens, or that opened it, loses its handle to it
unless same-origin (OAuth and payment pop-ups that report back through
`window.opener`); and `CORP: same-origin` stops other sites embedding its files.
Keep it on the one route that needs it, and mind three consequences:

- **Workers.** A dedicated worker started by an isolated page must have its *script*
  served with a compatible COEP, or it does not start at all. Put worker scripts
  (and the scripts that spawn threads, such as onnxruntime-web’s `.mjs`) under the
  isolated sub-path. Same-origin page scripts, wasm and data elsewhere need nothing.
- **Navigation.** Isolation is fixed when a document loads: a client-side route
  change into the isolated path does not isolate the page; link to it with a full
  navigation.
- **SPA fallback.** An unknown path under the isolated sub-path falls back to the
  app’s root `index.html`, served isolated; give the isolated page its own
  `index.html` and link to that.

The three-callable middleware is pure ASGI, as every enlace middleware is.

### Module Attributes

| [`CROSS_ORIGIN_ISOLATION_HEADERS`](#enlace.response_headers.CROSS_ORIGIN_ISOLATION_HEADERS)   | What `cross_origin_isolated` sends.                                                                                                                                        |
|-----------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`FORBIDDEN_HEADERS`](#enlace.response_headers.FORBIDDEN_HEADERS)                | replacing them would corrupt the response (framing, encoding, type), clobber the platform's session and CSRF cookies, or act on the whole origin, i.e. on every other app. |

### Functions

| [`add_response_headers`](#enlace.response_headers.add_response_headers)(parent, config)   | Install [`ResponseHeadersMiddleware`](#enlace.response_headers.ResponseHeadersMiddleware) on *parent* if an app declares any.   |
|-----------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------|
| [`app_header_table`](#enlace.response_headers.app_header_table)(app)                  | Sub-path -> headers for an app: isolation, then `[response_headers]` on top.                                             |
| [`app_mounts`](#enlace.response_headers.app_mounts)(app)                        | The path prefixes an app answers under: its page and its API, root excluded.                                             |
| [`header_rules_for_app`](#enlace.response_headers.header_rules_for_app)(app)              | The app's headers as `(mount, absolute path prefix, headers)` rules.                                                     |
| [`merge_headers`](#enlace.response_headers.merge_headers)(\*layers)                | Merge header dicts, later layers winning, names compared case-insensitively.                                             |
| [`normalize_subpath`](#enlace.response_headers.normalize_subpath)(key)                 | A sub-path key made canonical: no slash at either end, `""` for the whole app.                                           |
| [`path_under`](#enlace.response_headers.path_under)(prefix, path)               | Whether *path* is *prefix* or below it, by whole segments.                                                               |
| [`validate_cross_origin_isolated`](#enlace.response_headers.validate_cross_origin_isolated)(value)  | Check `cross_origin_isolated`: a bool, or a list of sub-paths (normalised).                                              |
| [`validate_response_headers`](#enlace.response_headers.validate_response_headers)(table)       | Check an app.toml `[response_headers]` table; keys come back normalised.                                                 |

### Classes

| [`ResponseHeadersMiddleware`](#enlace.response_headers.ResponseHeadersMiddleware)(app, \*, rules[, ...])   | Pure-ASGI middleware adding declared headers to responses under path prefixes.   |
|-----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------|

### enlace.response_headers.CROSS_ORIGIN_ISOLATION_HEADERS *: [dict](https://docs.python.org/3/builtins/stdtypes.html#dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [str](https://docs.python.org/3/builtins/stdtypes.html#str)]* *= {'Cross-Origin-Embedder-Policy': 'require-corp', 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Resource-Policy': 'same-origin'}*

What `cross_origin_isolated` sends. `require-corp` rather than `credentialless`
because Safari supports only the former; an app can override it per sub-path.

### enlace.response_headers.FORBIDDEN_HEADERS *: [frozenset](https://docs.python.org/3/builtins/stdtypes.html#frozenset)[[str](https://docs.python.org/3/builtins/stdtypes.html#str)]* *= frozenset({'clear-site-data', 'connection', 'content-encoding', 'content-length', 'content-range', 'content-type', 'keep-alive', 'location', 'service-worker-allowed', 'set-cookie', 'strict-transport-security', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'www-authenticate'})*

replacing them would corrupt the
response (framing, encoding, type), clobber the platform’s session and CSRF
cookies, or act on the whole origin, i.e. on every other app.

* **Type:**
  Header names an app may not declare (lower-case)

### *class* enlace.response_headers.ResponseHeadersMiddleware(app, , rules, mounts=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Pure-ASGI middleware adding declared headers to responses under path prefixes.

*rules* are `(mount, prefix, headers)` triples on full request paths. A request
is owned by the longest of *mounts* covering it (every app’s mounts, so an app
with a shorter mount never reaches a longer one’s paths); only the rules of that
mount apply, longer prefixes last, so the most specific wins a header both
declare. Websockets and lifespan pass through untouched.

```pycon
>>> import asyncio
>>> async def app(scope, receive, send):
...     await send({"type": "http.response.start", "status": 200, "headers": []})
...     await send({"type": "http.response.body", "body": b""})
>>> mw = ResponseHeadersMiddleware(app, rules=[("/a", "/a", {"X-A": "1"})])
>>> sent = []
>>> async def send(m): sent.append(m)
>>> asyncio.run(mw({"type": "http", "path": "/a/b"}, None, send))
>>> sent[0]["headers"]
[(b'x-a', b'1')]
```

#### headers_for(path)

The headers a response to *path* gets (empty when no rule covers it).

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### enlace.response_headers.add_response_headers(parent, config)

Install [`ResponseHeadersMiddleware`](#enlace.response_headers.ResponseHeadersMiddleware) on *parent* if an app declares any.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### enlace.response_headers.app_header_table(app)

Sub-path -> headers for an app: isolation, then `[response_headers]` on top.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]]

### enlace.response_headers.app_mounts(app)

The path prefixes an app answers under: its page and its API, root excluded.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### enlace.response_headers.header_rules_for_app(app)

The app’s headers as `(mount, absolute path prefix, headers)` rules.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]]]

### enlace.response_headers.merge_headers(\*layers)

Merge header dicts, later layers winning, names compared case-insensitively.

```pycon
>>> merge_headers({"X-A": "1", "X-B": "2"}, {"x-a": "3"})
{'X-B': '2', 'x-a': '3'}
```

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### enlace.response_headers.normalize_subpath(key)

A sub-path key made canonical: no slash at either end, `""` for the whole app.

```pycon
>>> normalize_subpath("/lab/"), normalize_subpath("lab"), normalize_subpath("/")
('lab', 'lab', '')
```

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### enlace.response_headers.path_under(prefix, path)

Whether *path* is *prefix* or below it, by whole segments.

```pycon
>>> path_under("/a/lab", "/a/lab/x"), path_under("/a/lab", "/a/labels")
(True, False)
```

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### enlace.response_headers.validate_cross_origin_isolated(value)

Check `cross_origin_isolated`: a bool, or a list of sub-paths (normalised).

```pycon
>>> validate_cross_origin_isolated(["/lab/"])
['lab']
```

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool) | [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### enlace.response_headers.validate_response_headers(table)

Check an app.toml `[response_headers]` table; keys come back normalised.

```pycon
>>> validate_response_headers({"/lab/": {"X-A": "1"}})
{'lab': {'X-A': '1'}}
```

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]]
