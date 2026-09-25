# enlace.analytics

Privacy-first page-view analytics, turned on per app.

An app opts in from its own `app.toml`:

```default
[analytics]
mode = "privacy"
```

Without that table the app records nothing. With it, the enlace server that
already serves the app counts its page views \*\*on the server, from its own
request handling\*\*: no JavaScript, no beacon, no third-party request, and
nothing written to the visitor’s device. The page a visitor receives is
byte-for-byte what it would be without analytics.

What is kept: daily aggregates per app, one counter set per dimension:

- `pageviews`: total HTML page views;
- `paths`: views per page path, relative to the app, with query string and
  fragment dropped, and segments that look like an email, a UUID or a token
  replaced by `:id` (best effort — see [`normalize_path()`](#enlace.analytics.normalize_path));
- `referrers`: the referring *host name* only, or `(direct)` /
  `(internal)` / `(ip)`;
- `languages`: the primary subtag of `Accept-Language` (`fr`, `en`);
- `devices`: `mobile` / `tablet` / `desktop`;
- `bot_hits`: page requests from crawlers and vulnerability scanners,
  counted apart and nowhere else.

Dimensions are stored as separate marginal counts and are **never crossed**
(no “path × language × device” table), so a rare combination cannot single
out one visitor on a low-traffic page. No IP address is read, and no
identifier of any kind is stored. Unique visitors are deliberately not
counted: see `misc/docs/privacy_analytics.md` for why, and for how this
design maps onto the CNIL’s audience-measurement exemption.

Visitors can object: `DNT: 1` and `Sec-GPC: 1` are honoured, and
`/_analytics/opt-out` is a page a privacy notice can link to. It sets a
single first-party opt-out cookie when (and only when) the visitor asks.

Storage is any `MutableMapping[str, dict]` (the `store` seam — a `dol`
store drops in unchanged); the default is [`JsonFileStore`](#enlace.analytics.JsonFileStore) under
`~/.local/share/enlace/analytics`. Each worker process writes only its own
records (`{app}/{day}/{writer}`), so workers never race on a record, and
readers sum the writers. Writes happen off the event loop, in a background
task started with the server; so does the daily maintenance, which purges
records past `retention_days` (whether or not anything was viewed that day)
and compacts each finished day’s writer records into one.

### Module Attributes

| [`MAX_RETENTION_DAYS`](#enlace.analytics.MAX_RETENTION_DAYS)     | The CNIL's ceiling for keeping audience-measurement data is 25 months; 750 days stays under it for any 25 consecutive months.   |
|-------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------|
| [`ANALYTICS_ROUTE_PREFIX`](#enlace.analytics.ANALYTICS_ROUTE_PREFIX) | Where the platform's analytics routes live (the opt-out page).                                                                  |
| [`MERGED`](#enlace.analytics.MERGED)                 | The writer id of a finished day's compacted record.                                                                             |

### Functions

| [`analytics_report`](#enlace.analytics.analytics_report)([app, days, config, store])       | Per-app report for the last `days` days: daily series + totals.                                                     |
|-----------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------|
| [`apps_with_data`](#enlace.analytics.apps_with_data)(store)                              | Names of the apps that have any analytics records.                                                                  |
| [`daily_counts`](#enlace.analytics.daily_counts)(store, app, \*[, days, today, ...])   | One merged record per day for the last `days` days, oldest first.                                                   |
| [`default_analytics_store`](#enlace.analytics.default_analytics_store)([config])                  | The default store a platform config points at.                                                                      |
| [`default_store_path`](#enlace.analytics.default_store_path)()                               | `$XDG_DATA_HOME/enlace/analytics`, else `~/.local/share/enlace/analytics`.                                          |
| [`device_class`](#enlace.analytics.device_class)(user_agent, \*[, client_hint_mobile]) | `bot`, `tablet`, `mobile` or `desktop`, from the User-Agent alone.                                                  |
| [`has_opted_out`](#enlace.analytics.has_opted_out)(headers, \*, cookie_name)            | `DNT: 1`, `Sec-GPC: 1`, or the platform's opt-out cookie.                                                           |
| [`is_page_request`](#enlace.analytics.is_page_request)(method, headers)                   | Whether a request is a browser loading a page (not an asset, API or prefetch).                                      |
| [`is_page_response`](#enlace.analytics.is_page_response)(status, headers)                  | Whether a response delivered a page: a 200 HTML body, or a 304 revalidation.                                        |
| [`looks_like_probe`](#enlace.analytics.looks_like_probe)(path)                             | A scanner's request, not a page: a dot-segment or a non-HTML file name.                                             |
| [`make_analytics`](#enlace.analytics.make_analytics)(config, \*[, store])                | The platform's analytics, or `None` when there is nothing to do.                                                    |
| [`normalize_path`](#enlace.analytics.normalize_path)(path)                               | A page path fit to store and to print.                                                                              |
| [`primary_language`](#enlace.analytics.primary_language)(accept_language)                  | Primary subtag of the first `Accept-Language` entry (`fr-FR` → `fr`).                                               |
| [`referrer_domain`](#enlace.analytics.referrer_domain)(referer, \*, host)                 | The referring host name only.                                                                                       |
| [`summarize`](#enlace.analytics.summarize)(daily)                                   | Totals over a [`daily_counts()`](#enlace.analytics.daily_counts) series: pageviews and each dimension. |

### Classes

| [`Analytics`](#enlace.analytics.Analytics)(config, counter, \*[, collecting])      | The analytics feature for one platform: counter, attribution, routes.   |
|----------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------|
| [`AppAnalyticsConfig`](#enlace.analytics.AppAnalyticsConfig)(\*\*data)                      | An app's `[analytics]` table in `app.toml`.                             |
| [`JsonFileStore`](#enlace.analytics.JsonFileStore)(root)                               | `key -> dict`, one JSON file per key at `{root}/{key}.json`.            |
| [`PageAttribution`](#enlace.analytics.PageAttribution)(apps, \*[, landing_app, ...])     | Maps a request path to `(app, app-relative path)`, or `None`.           |
| [`PageViewCounter`](#enlace.analytics.PageViewCounter)(store, \*[, retention_days, ...]) | Buffers one worker's daily aggregates and maintains the `store`.        |
| [`PageViewMiddleware`](#enlace.analytics.PageViewMiddleware)(app, \*, counter, attribute)   | Pure-ASGI middleware counting page views of the apps that opted in.     |
| [`PlatformAnalyticsConfig`](#enlace.analytics.PlatformAnalyticsConfig)(\*\*data)                 | The platform's `[analytics]` table in `platform.toml`.                  |

### enlace.analytics.ANALYTICS_ROUTE_PREFIX *= '/_analytics'*

Where the platform’s analytics routes live (the opt-out page).

### *class* enlace.analytics.Analytics(config, counter, , collecting=True)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

The analytics feature for one platform: counter, attribution, routes.

`collecting` is False when no app opted in but old data exists: then only
the daily maintenance runs, so retention keeps being enforced after every
app has turned analytics off.

#### add_middleware(parent)

Install the counting (and lifespan-owning) middleware.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### add_routes(parent)

Register the opt-out page. Call before any catch-all `/` mount.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### *class* enlace.analytics.AppAnalyticsConfig(\*\*data)

Bases: `BaseModel`

An app’s `[analytics]` table in `app.toml`. Absent means `none`.

Strict (unknown keys and modes are errors), so a typo fails at discovery
instead of silently collecting nothing — or something else.

#### *property* enabled *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

Whether this app’s page views are counted.

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {'extra': 'forbid'}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.analytics.JsonFileStore(root)

Bases: [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)

`key -> dict`, one JSON file per key at `{root}/{key}.json`.

Keys are `/`-separated relative paths of URL-safe segments. Writes are
atomic (temp file + `os.replace`), so a reader never sees half a record.
Stdlib only; swap in any `MutableMapping` (e.g. a `dol` store over S3)
through the `store` argument of [`make_analytics()`](#enlace.analytics.make_analytics) or
`build_backend`. Two optional extras the maintenance uses when present:
[`exclusive()`](#enlace.analytics.JsonFileStore.exclusive) (a cross-process lock) and [`sweep_temp_files()`](#enlace.analytics.JsonFileStore.sweep_temp_files).

#### exclusive()

Try to take the store-wide maintenance lock; yield whether we got it.

Non-blocking: a worker that loses the race skips this round of
maintenance, it does not wait. `False` where `fcntl` is unavailable.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`bool`](https://docs.python.org/3/builtins/functions.html#bool)]

#### sweep_temp_files(, older_than=3600)

Delete temp files a killed write left behind; return how many.

* **Return type:**
  [`int`](https://docs.python.org/3/builtins/functions.html#int)

### enlace.analytics.MAX_RETENTION_DAYS *= 750*

The CNIL’s ceiling for keeping audience-measurement data is 25 months;
750 days stays under it for any 25 consecutive months.

### enlace.analytics.MERGED *= 'merged'*

The writer id of a finished day’s compacted record.

### *class* enlace.analytics.PageAttribution(apps, , landing_app=None, exclude_prefixes=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Maps a request path to `(app, app-relative path)`, or `None`.

Platform paths (`exclude_prefixes`) never count. Otherwise the longest
matching app mount (`/{name}/` or its route prefix) wins, whether or not
that app opted in — a page of an app that did not must never fall through
to one that did. Anything else goes to the landing app, if it opted in.

### *class* enlace.analytics.PageViewCounter(store, \*, retention_days=395, timezone='UTC', flush_interval_seconds=10.0, max_values_per_dimension=500, writer=None, clock=<built-in function time>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Buffers one worker’s daily aggregates and maintains the `store`.

Each worker writes only under its own `writer` id (derived from its
process id at first use, so workers forked from one preloaded app still
differ), overwriting its own cumulative record for the day: no worker ever
read-modify-writes a record another is writing.

Two schedules. [`tick()`](#enlace.analytics.PageViewCounter.tick) — run by the server’s background task, in a
thread — flushes every `flush_interval_seconds` and runs [`maintain()`](#enlace.analytics.PageViewCounter.maintain)
once per day. Without that task (`background=False`: a bare ASGI host, a
test client outside `with`), [`record_view()`](#enlace.analytics.PageViewCounter.record_view) flushes inline instead.

#### compact(, before)

Fold each day’s writer records (days before `before`) into one.

Every worker restart starts a new writer record, so a finished day can
hold dozens of small files; this leaves one per app per day. Crash-safe:
the merged record lists its `sources`, so a source that survived an
interrupted run is deleted without being counted twice. Call only under
the store’s exclusive lock. Returns how many records were folded.

* **Return type:**
  [`int`](https://docs.python.org/3/builtins/functions.html#int)

#### flush()

Write every changed record, and forget days that are over.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### maintain(, today=None)

Purge expired records; compact finished days; sweep stale temp files.

Purging is idempotent, so every worker may do it. Compaction is not, so
it runs only under the store’s `exclusive()` lock, and only on days at
least two days old, which no writer touches any more.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### maybe_flush()

Flush if the flush interval has elapsed since the last one.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### purge_expired(, today=None)

Delete records older than the retention window; return how many.

The window is `retention_days` days, today included.

* **Return type:**
  [`int`](https://docs.python.org/3/builtins/functions.html#int)

#### record_bot_hit(app)

Count a request from a crawler or scanner, and nothing else about it.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### record_view(app, , path, referrer, language, device)

Count one page view of `app` (a `bot` device counts as a bot hit).

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### *async* run_background()

The server-lifetime loop: [`tick()`](#enlace.analytics.PageViewCounter.tick) in a thread, forever.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### tick()

One background round: flush when due, maintain once per day.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### today()

The current day, ISO format, in the configured timezone.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

#### *property* writer *: [str](https://docs.python.org/3/builtins/stdtypes.html#str)*

fixed if given, else `{pid}-{random}`.

* **Type:**
  This process’s writer id

### *class* enlace.analytics.PageViewMiddleware(app, , counter, attribute, settings=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Pure-ASGI middleware counting page views of the apps that opted in.

It only observes: the request and response pass through unchanged, and
nothing is added to the page. It also owns the counter’s lifetime: the
background flush/maintenance task starts with the server’s lifespan and
the last flush happens at shutdown.

### *class* enlace.analytics.PlatformAnalyticsConfig(\*\*data)

Bases: `BaseModel`

The platform’s `[analytics]` table in `platform.toml`.

Every field has a working default; the table is only needed to change one.
Validated at config load, so `enlace check` catches a bad value before a
boot does.

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {'extra': 'forbid'}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### enlace.analytics.analytics_report(app='', , days=30, config=None, store=None)

Per-app report for the last `days` days: daily series + totals.

With no `app`, reports every app that has data. `config` defaults to
`platform.toml` in the current directory (only its `[analytics]` table
is used; no app is imported).

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### enlace.analytics.apps_with_data(store)

Names of the apps that have any analytics records.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### enlace.analytics.daily_counts(store, app, , days=30, today=None, timezone='UTC', index=None)

One merged record per day for the last `days` days, oldest first.

Days with no data are included, with zero counts, so the series has no
gaps. Each item is `{"date": ..., "pageviews": ..., "bot_hits": ...,
"paths": {...}, "referrers": {...}, "languages": {...}, "devices": {...}}`.
Pass `index` (from one scan) when reading several apps.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)]

### enlace.analytics.default_analytics_store(config=None)

The default store a platform config points at.

* **Return type:**
  [`JsonFileStore`](#enlace.analytics.JsonFileStore)

### enlace.analytics.default_store_path()

`$XDG_DATA_HOME/enlace/analytics`, else `~/.local/share/enlace/analytics`.

* **Return type:**
  [`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)

### enlace.analytics.device_class(user_agent, , client_hint_mobile='')

`bot`, `tablet`, `mobile` or `desktop`, from the User-Agent alone.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### enlace.analytics.has_opted_out(headers, , cookie_name)

`DNT: 1`, `Sec-GPC: 1`, or the platform’s opt-out cookie.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### enlace.analytics.is_page_request(method, headers)

Whether a request is a browser loading a page (not an asset, API or prefetch).

Modern browsers say so directly (`Sec-Fetch-Dest: document`); older ones
are recognised by `Accept: text/html`. Prefetches and prerenders are not
views.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### enlace.analytics.is_page_response(status, headers)

Whether a response delivered a page: a 200 HTML body, or a 304 revalidation.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### enlace.analytics.looks_like_probe(path)

A scanner’s request, not a page: a dot-segment or a non-HTML file name.

An SPA answers `/app/wp-admin/setup.php` or `/app/.env` with its
`index.html`, so a browser-like scanner would otherwise pass as a reader.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### enlace.analytics.make_analytics(config, , store=None)

The platform’s analytics, or `None` when there is nothing to do.

Nothing to do means no app opted in *and* the store holds no data. If data
remains after every app opted out, maintenance alone still runs, so it is
purged on schedule. `store` is the storage seam: any
`MutableMapping[str, dict]`. Default: a [`JsonFileStore`](#enlace.analytics.JsonFileStore) at
`[analytics].store_path` (or [`default_store_path()`](#enlace.analytics.default_store_path)).

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Analytics`](#enlace.analytics.Analytics)]

### enlace.analytics.normalize_path(path)

A page path fit to store and to print.

`index.html` folds into its directory; non-printable characters (terminal
escapes) are dropped; segments that look like an email, a UUID or a long
token become `:id`; the result is capped at 200 characters. Redaction is
best effort: an app that puts personal data in its URL paths should not
turn analytics on.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### enlace.analytics.primary_language(accept_language)

Primary subtag of the first `Accept-Language` entry (`fr-FR` → `fr`).

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### enlace.analytics.referrer_domain(referer, , host)

The referring host name only.

`(direct)` if none, `(internal)` if this site, `(ip)` for an address
(it may be a person’s own machine), `(unknown)` for anything that is not
a plain host name.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### enlace.analytics.summarize(daily)

Totals over a [`daily_counts()`](#enlace.analytics.daily_counts) series: pageviews and each dimension.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)
