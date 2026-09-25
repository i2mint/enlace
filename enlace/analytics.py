"""Privacy-first page-view analytics, turned on per app.

An app opts in from its own ``app.toml``::

    [analytics]
    mode = "privacy"

Without that table the app records nothing. With it, the enlace server that
already serves the app counts its page views **on the server, from its own
request handling**: no JavaScript, no beacon, no third-party request, and
nothing written to the visitor's device. The page a visitor receives is
byte-for-byte what it would be without analytics.

What is kept: daily aggregates per app, one counter set per dimension:

- ``pageviews``: total HTML page views;
- ``paths``: views per page path, relative to the app, with query string and
  fragment dropped (so no campaign IDs or tokens from URLs get in);
- ``referrers``: the referring *domain* only, or ``(direct)`` / ``(internal)``;
- ``languages``: the primary subtag of ``Accept-Language`` (``fr``, ``en``);
- ``devices``: ``mobile`` / ``tablet`` / ``desktop``;
- ``bot_hits``: page requests from crawlers, counted apart and nowhere else.

Dimensions are stored as separate marginal counts and are **never crossed**
(no "path × language × device" table), so a rare combination cannot single
out one visitor on a low-traffic page. No IP address is read, and no
identifier of any kind is stored. Unique visitors are deliberately not
counted: see ``misc/docs/privacy_analytics.md`` for why, and for how this
design maps onto the CNIL's audience-measurement exemption.

Visitors can object: ``DNT: 1`` and ``Sec-GPC: 1`` are honoured, and
``/_analytics/opt-out`` is a page a privacy notice can link to. It sets a
single first-party opt-out cookie when (and only when) the visitor asks.

Storage is any ``MutableMapping[str, dict]`` (the ``store`` seam — a ``dol``
store drops in unchanged); the default is :class:`JsonFileStore` under
``~/.local/share/enlace/analytics``. Each worker process writes only its own
records (``{app}/{day}/{writer}``), so several workers never race on a file,
and readers sum the writers. Records older than ``retention_days`` are purged.
"""

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, MutableMapping
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Optional, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from enlace.base import AppConfig, PlatformConfig

_logger = logging.getLogger("enlace.analytics")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AnalyticsMode = Literal["none", "privacy"]

#: The CNIL's ceiling for keeping audience-measurement data: 25 months.
MAX_RETENTION_DAYS = 25 * 31

#: Where the platform's analytics routes live (the opt-out page).
ANALYTICS_ROUTE_PREFIX = "/_analytics"


class AppAnalyticsConfig(BaseModel):
    """An app's ``[analytics]`` table in ``app.toml``. Absent means ``none``.

    Strict (unknown keys and modes are errors), so a typo fails at discovery
    instead of silently collecting nothing — or something else.
    """

    model_config = ConfigDict(extra="forbid")

    mode: AnalyticsMode = "none"

    @property
    def enabled(self) -> bool:
        """Whether this app's page views are counted."""
        return self.mode != "none"


class PlatformAnalyticsConfig(BaseModel):
    """The platform's ``[analytics]`` table in ``platform.toml``.

    Every field has a working default; the table is only needed to change one.
    """

    model_config = ConfigDict(extra="forbid")

    store_path: Optional[Path] = Field(
        default=None,
        description="Directory of the default JSON store "
        "(default: $XDG_DATA_HOME/enlace/analytics, i.e. ~/.local/share/...).",
    )
    retention_days: int = Field(
        default=395,
        ge=1,
        le=MAX_RETENTION_DAYS,
        description="Days of daily aggregates kept; older ones are purged. "
        "Capped at 25 months (the CNIL ceiling).",
    )
    timezone: str = Field(
        default="UTC", description="IANA zone whose midnight starts a new day."
    )
    flush_interval_seconds: float = Field(
        default=10.0,
        ge=0,
        description="How often a worker writes its buffered counts (0 = every view).",
    )
    max_values_per_dimension: int = Field(
        default=500,
        ge=1,
        description="Distinct values kept per dimension per day and worker; "
        "the rest are counted under '(other)'. Bounds junk-URL floods.",
    )
    honor_opt_out_signals: bool = Field(
        default=True, description="Skip requests carrying DNT: 1 or Sec-GPC: 1."
    )
    exclude_prefixes: tuple[str, ...] = Field(
        default=("/_", "/auth/"),
        description="Platform paths never attributed to the landing app.",
    )
    opt_out_cookie: str = "enlace_analytics_opt_out"


def default_store_path() -> Path:
    """``$XDG_DATA_HOME/enlace/analytics``, else ``~/.local/share/enlace/analytics``."""
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "enlace" / "analytics"


# ---------------------------------------------------------------------------
# Storage: the default store (any MutableMapping[str, dict] will do)
# ---------------------------------------------------------------------------

_KEY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class JsonFileStore(MutableMapping):
    """``key -> dict``, one JSON file per key at ``{root}/{key}.json``.

    Keys are ``/``-separated relative paths. Writes are atomic (temp file +
    ``os.replace``), so a reader never sees half a record. Stdlib only; swap in
    any ``MutableMapping`` (e.g. a ``dol`` store over S3) through the ``store``
    argument of :func:`make_analytics` or ``build_backend``.
    """

    _SUFFIX = ".json"

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser()

    def _path(self, key: str) -> Path:
        parts = key.split("/")
        if not all(_KEY_SEGMENT_RE.match(p) and p not in (".", "..") for p in parts):
            raise KeyError(f"invalid key: {key!r}")
        return self.root.joinpath(*parts).with_name(parts[-1] + self._SUFFIX)

    def __getitem__(self, key: str) -> dict:
        try:
            return json.loads(self._path(key).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(key) from None

    def __setitem__(self, key: str, value: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(value, f, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp)
            raise

    def __delitem__(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            raise KeyError(key) from None
        with suppress(OSError):
            path.parent.rmdir()  # drop the day directory once it is empty

    def __iter__(self) -> Iterator[str]:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.rglob(f"*{self._SUFFIX}")):
            if path.name.startswith("."):
                continue
            yield path.relative_to(self.root).with_suffix("").as_posix()

    def __len__(self) -> int:
        return sum(1 for _ in self)


def _record_key(app: str, day: str, writer: str) -> str:
    return f"{app}/{day}/{writer}"


def _split_key(key: str) -> Optional[tuple[str, str, str]]:
    parts = key.split("/")
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 else None


# ---------------------------------------------------------------------------
# Classifying a request (pure functions)
# ---------------------------------------------------------------------------

OTHER = "(other)"
DIRECT = "(direct)"
INTERNAL = "(internal)"
UNKNOWN = "(unknown)"

_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|scrap|curl|wget|python-|httpx|go-http|java/|"
    r"headless|lighthouse|pingdom|monitor|preview|facebookexternalhit|embedly",
    re.IGNORECASE,
)
_TABLET_RE = re.compile(r"ipad|tablet|kindle|silk|playbook|android(?!.*mobile)", re.I)
_MOBILE_RE = re.compile(
    r"mobi|iphone|ipod|android|blackberry|opera mini|iemobile", re.I
)
_LANG_RE = re.compile(r"^[a-z]{2,3}$")
_MAX_PATH_LENGTH = 200


def is_page_request(method: str, headers: dict[str, str]) -> bool:
    """Whether a request is a browser loading a page (not an asset, API or prefetch).

    Modern browsers say so directly (``Sec-Fetch-Dest: document``); older ones
    are recognised by ``Accept: text/html``. Prefetches and prerenders are not
    views.
    """
    if method != "GET":
        return False
    purpose = headers.get("sec-purpose", "") + headers.get("purpose", "")
    if "prefetch" in purpose.lower():
        return False
    dest = headers.get("sec-fetch-dest")
    if dest is not None:
        return dest == "document"
    return "text/html" in headers.get("accept", "").lower()


def is_page_response(status: int, headers: dict[str, str]) -> bool:
    """Whether a response delivered a page: a 200 HTML body, or a 304 revalidation."""
    if status == 304:
        return True
    return status == 200 and headers.get("content-type", "").lower().startswith(
        "text/html"
    )


def has_opted_out(headers: dict[str, str], *, cookie_name: str) -> bool:
    """``DNT: 1``, ``Sec-GPC: 1``, or the platform's opt-out cookie."""
    if headers.get("dnt") == "1" or headers.get("sec-gpc") == "1":
        return True
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == cookie_name and value == "1":
            return True
    return False


def device_class(user_agent: str, *, client_hint_mobile: str = "") -> str:
    """``bot``, ``tablet``, ``mobile`` or ``desktop``, from the User-Agent alone."""
    if not user_agent or _BOT_RE.search(user_agent):
        return "bot"
    if _TABLET_RE.search(user_agent):
        return "tablet"
    if client_hint_mobile == "?1" or _MOBILE_RE.search(user_agent):
        return "mobile"
    return "desktop"


def primary_language(accept_language: str) -> str:
    """Primary subtag of the first ``Accept-Language`` entry (``fr-FR`` → ``fr``)."""
    first = accept_language.split(",", 1)[0].split(";", 1)[0].strip().lower()
    tag = first.split("-", 1)[0]
    return tag if _LANG_RE.match(tag) else UNKNOWN


def referrer_domain(referer: str, *, host: str) -> str:
    """The referring host only; ``(direct)`` if none, ``(internal)`` if this site."""
    if not referer:
        return DIRECT
    try:
        hostname = urlsplit(referer).hostname
    except ValueError:
        return UNKNOWN
    if not hostname:
        return UNKNOWN
    own = host.rsplit(":", 1)[0].lower() if host else ""
    return INTERNAL if hostname == own else hostname


def normalize_path(path: str) -> str:
    """A page path fit to store: ``index.html`` folded into its directory, capped."""
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    return (path or "/")[:_MAX_PATH_LENGTH]


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

DIMENSIONS = ("paths", "referrers", "languages", "devices")


def _empty_record() -> dict:
    return {"pageviews": 0, "bot_hits": 0, **{d: {} for d in DIMENSIONS}}


def _copy_record(rec: dict) -> dict:
    return {**rec, **{d: dict(rec[d]) for d in DIMENSIONS}}


class PageViewCounter:
    """Buffers one worker's daily aggregates and flushes them to ``store``.

    Each counter writes only under its own ``writer`` id, overwriting its own
    cumulative record for the day, so concurrent workers never read-modify-write
    a shared record. A flush happens at most every ``flush_interval_seconds``
    (on the next view), on :meth:`flush`, and at server shutdown.
    """

    def __init__(
        self,
        store: MutableMapping,
        *,
        retention_days: int = 395,
        timezone: str = "UTC",
        flush_interval_seconds: float = 10.0,
        max_values_per_dimension: int = 500,
        writer: Optional[str] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.retention_days = retention_days
        self._tz = ZoneInfo(timezone)
        self._flush_interval = flush_interval_seconds
        self._max_values = max_values_per_dimension
        self.writer = writer or uuid.uuid4().hex[:12]
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], dict] = {}
        self._dirty: set[tuple[str, str]] = set()
        self._last_flush = clock()
        self._purged_through: Optional[str] = None

    def today(self) -> str:
        """The current day, ISO format, in the configured timezone."""
        return datetime.fromtimestamp(self._clock(), tz=self._tz).date().isoformat()

    def record_view(
        self,
        app: str,
        *,
        path: str,
        referrer: str,
        language: str,
        device: str,
    ) -> None:
        """Count one page view of ``app`` (a ``bot`` device counts as a bot hit)."""
        with self._lock:
            rec = self._record_for(app)
            if device == "bot":
                rec["bot_hits"] += 1
            else:
                rec["pageviews"] += 1
                for dim, value in zip(DIMENSIONS, (path, referrer, language, device)):
                    self._bump(rec[dim], value)
        self.maybe_flush()

    def _record_for(self, app: str) -> dict:
        key = (app, self.today())
        self._dirty.add(key)
        if key not in self._records:
            self._records[key] = _empty_record()
        return self._records[key]

    def _bump(self, counts: dict, value: str) -> None:
        if value not in counts and len(counts) >= self._max_values:
            value = OTHER
        counts[value] = counts.get(value, 0) + 1

    def maybe_flush(self) -> None:
        """Flush if the flush interval has elapsed since the last one."""
        if self._clock() - self._last_flush >= self._flush_interval:
            self.flush()

    def flush(self) -> None:
        """Write every changed record, forget finished days, purge expired data."""
        with self._lock:
            today = self.today()
            pending = {k: _copy_record(self._records[k]) for k in self._dirty}
            self._dirty.clear()
            self._last_flush = self._clock()
            # A finished day's final state is in ``pending`` (or already written).
            for key in [k for k in self._records if k[1] != today]:
                del self._records[key]
        for (app, day), rec in pending.items():
            try:
                self.store[_record_key(app, day, self.writer)] = rec
            except Exception:  # analytics must never break serving
                _logger.warning(
                    "analytics: could not write %s/%s", app, day, exc_info=True
                )
        if self._purged_through != today:
            self.purge_expired(today=today)

    def purge_expired(self, *, today: Optional[str] = None) -> int:
        """Delete records older than the retention window; return how many."""
        today = today or self.today()
        cutoff = (
            date.fromisoformat(today) - timedelta(days=self.retention_days)
        ).isoformat()
        removed = 0
        try:
            for key in list(self.store):
                parts = _split_key(key)
                if parts is not None and parts[1] < cutoff:
                    try:
                        del self.store[key]
                        removed += 1
                    except KeyError:  # another worker got there first
                        pass
        except Exception:
            _logger.warning("analytics: retention purge failed", exc_info=True)
            return removed
        self._purged_through = today
        return removed


# ---------------------------------------------------------------------------
# Collecting: the middleware
# ---------------------------------------------------------------------------


def _headers(scope) -> dict[str, str]:
    return {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in scope.get("headers", [])
    }


class PageViewMiddleware:
    """Pure-ASGI middleware counting page views of the apps that opted in.

    It only observes: the request and response pass through unchanged, and
    nothing is added to the page. A page view is attributed to the app whose
    mount (``/{name}/`` or its route prefix) is the longest match; other paths
    go to the landing app, except the platform's own (``exclude_prefixes``).
    """

    def __init__(
        self,
        app,
        *,
        counter: PageViewCounter,
        apps: Sequence["AppConfig"],
        landing_app: Optional[str] = None,
        settings: Optional[PlatformAnalyticsConfig] = None,
    ):
        self.app = app
        self._counter = counter
        self._settings = settings or PlatformAnalyticsConfig()
        enabled = {a.name for a in apps if a.analytics.enabled}
        prefixes = []
        for a in apps:
            for prefix in {f"/{a.name}/", a.route_prefix.rstrip("/") + "/"}:
                prefixes.append((prefix, a.name))
        # Every app's prefixes (not just enabled ones): a disabled app's page
        # must never fall through to an enabled landing app.
        self._prefixes = sorted(prefixes, key=lambda p: -len(p[0]))
        self._enabled = enabled
        self._landing = landing_app if landing_app in enabled else None

    def attribute(self, path: str) -> Optional[tuple[str, str]]:
        """``(app, app-relative path)`` for a page to count, or ``None``."""
        for prefix, name in self._prefixes:
            if path.startswith(prefix):
                if name not in self._enabled:
                    return None
                return name, normalize_path("/" + path[len(prefix) :])
        if self._landing and not path.startswith(self._settings.exclude_prefixes):
            return self._landing, normalize_path(path)
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, self._flushing_on_shutdown(receive), send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        target = self.attribute(scope.get("path", ""))
        headers = _headers(scope) if target else {}
        if (
            target is None
            or not is_page_request(scope.get("method", ""), headers)
            or (
                self._settings.honor_opt_out_signals
                and has_opted_out(headers, cookie_name=self._settings.opt_out_cookie)
            )
        ):
            await self.app(scope, receive, send)
            return

        async def observing_send(message):
            if message["type"] == "http.response.start":
                self._maybe_count(target, headers, message)
            await send(message)

        await self.app(scope, receive, observing_send)

    def _maybe_count(self, target, headers, message) -> None:
        try:
            response_headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in message.get("headers", [])
            }
            if not is_page_response(message["status"], response_headers):
                return
            app, path = target
            self._counter.record_view(
                app,
                path=path,
                referrer=referrer_domain(
                    headers.get("referer", ""), host=headers.get("host", "")
                ),
                language=primary_language(headers.get("accept-language", "")),
                device=device_class(
                    headers.get("user-agent", ""),
                    client_hint_mobile=headers.get("sec-ch-ua-mobile", ""),
                ),
            )
        except Exception:  # counting must never break a page
            _logger.warning("analytics: failed to count a page view", exc_info=True)

    def _flushing_on_shutdown(self, receive):
        async def wrapped():
            message = await receive()
            if message.get("type") == "lifespan.shutdown":
                try:
                    self._counter.flush()
                except Exception:
                    _logger.warning("analytics: final flush failed", exc_info=True)
            return message

        return wrapped


# ---------------------------------------------------------------------------
# The opt-out page
# ---------------------------------------------------------------------------

_OPT_OUT_TEXT = {
    "en": {
        "title": "Audience statistics",
        "about": "This site counts page views anonymously, on its own server: "
        "no tracker, no third party, nothing stored about you.",
        "out": "You have opted out: your visits are not counted.",
        "in": "Your visits are counted, anonymously.",
        "do_out": "Don't count my visits",
        "do_in": "Count my visits again",
    },
    "fr": {
        "title": "Statistiques de fréquentation",
        "about": "Ce site compte les pages vues de façon anonyme, sur son propre "
        "serveur : aucun traceur, aucun tiers, rien n'est conservé sur vous.",
        "out": "Vos visites ne sont plus comptées.",
        "in": "Vos visites sont comptées, de façon anonyme.",
        "do_out": "Ne plus compter mes visites",
        "do_in": "Compter à nouveau mes visites",
    },
}

#: How long the opt-out choice is remembered (13 months, the CNIL maximum).
_OPT_OUT_MAX_AGE = 13 * 31 * 24 * 3600


def _add_opt_out_route(parent: "FastAPI", settings: PlatformAnalyticsConfig) -> None:
    """``GET /_analytics/opt-out[?choice=out|in]``: a page a privacy notice links to.

    A plain page with two links, no script. Choosing "out" sets one first-party
    cookie (exempt from consent: it stores the visitor's refusal); "in" removes
    it. GET, so it works as a link and needs no CSRF token; the worst a forged
    link can do is stop counting someone.
    """
    import html

    from fastapi import Request
    from fastapi.responses import HTMLResponse

    cookie = settings.opt_out_cookie
    base = f"{ANALYTICS_ROUTE_PREFIX}/opt-out"

    @parent.get(base, include_in_schema=False)
    async def opt_out_page(request: Request, choice: str = "") -> HTMLResponse:
        lang = primary_language(request.headers.get("accept-language", ""))
        text = _OPT_OUT_TEXT.get(lang, _OPT_OUT_TEXT["en"])
        opted_out = request.cookies.get(cookie) == "1"
        if choice in ("out", "in"):
            opted_out = choice == "out"
        t = {k: html.escape(v) for k, v in text.items()}
        body = (
            f'<!doctype html><html lang="{lang if lang in _OPT_OUT_TEXT else "en"}">'
            '<head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{t['title']}</title></head>"
            '<body style="font-family:system-ui,sans-serif;max-width:36rem;'
            'margin:3rem auto;padding:0 1rem;line-height:1.5">'
            f"<h1>{t['title']}</h1><p>{t['about']}</p>"
            f"<p><strong>{t['out'] if opted_out else t['in']}</strong></p>"
            f'<p><a href="{base}?choice={"in" if opted_out else "out"}">'
            f"{t['do_in'] if opted_out else t['do_out']}</a></p></body></html>"
        )
        response = HTMLResponse(body, headers={"Cache-Control": "no-store"})
        if choice == "out":
            response.set_cookie(
                cookie,
                "1",
                max_age=_OPT_OUT_MAX_AGE,
                path="/",
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
            )
        elif choice == "in":
            response.delete_cookie(cookie, path="/")
        return response


# ---------------------------------------------------------------------------
# Wiring (called by build_backend)
# ---------------------------------------------------------------------------


class Analytics:
    """The analytics feature for one platform: its counter, routes and middleware."""

    def __init__(self, config: "PlatformConfig", counter: PageViewCounter):
        self.config = config
        self.settings = config.analytics
        self.counter = counter

    def add_routes(self, parent: "FastAPI") -> None:
        """Register the opt-out page. Call before any catch-all ``/`` mount."""
        _add_opt_out_route(parent, self.settings)
        parent.state.analytics = self

    def add_middleware(self, parent: "FastAPI") -> None:
        """Install the counting middleware (it only observes; order-insensitive)."""
        parent.add_middleware(
            PageViewMiddleware,
            counter=self.counter,
            apps=list(self.config.apps),
            landing_app=self.config.landing_app,
            settings=self.settings,
        )


def make_analytics(
    config: "PlatformConfig", *, store: Optional[MutableMapping] = None
) -> Optional[Analytics]:
    """The platform's analytics, or ``None`` when no app has opted in.

    ``store`` is the storage seam: any ``MutableMapping[str, dict]``. Default: a
    :class:`JsonFileStore` at ``[analytics].store_path`` (or
    :func:`default_store_path`).
    """
    if not any(a.analytics.enabled for a in config.apps):
        return None
    settings = config.analytics
    if store is None:
        store = JsonFileStore(settings.store_path or default_store_path())
    counter = PageViewCounter(
        store,
        retention_days=settings.retention_days,
        timezone=settings.timezone,
        flush_interval_seconds=settings.flush_interval_seconds,
        max_values_per_dimension=settings.max_values_per_dimension,
    )
    return Analytics(config, counter)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _merge(into: dict, rec: dict) -> None:
    into["pageviews"] += rec.get("pageviews", 0)
    into["bot_hits"] += rec.get("bot_hits", 0)
    for dim in DIMENSIONS:
        for value, n in (rec.get(dim) or {}).items():
            into[dim][value] = into[dim].get(value, 0) + n


def apps_with_data(store: MutableMapping) -> list[str]:
    """Names of the apps that have any analytics records."""
    return sorted({p[0] for k in store if (p := _split_key(k)) is not None})


def daily_counts(
    store: MutableMapping,
    app: str,
    *,
    days: int = 30,
    today: Optional[str] = None,
    timezone: str = "UTC",
) -> list[dict]:
    """One merged record per day for the last ``days`` days, oldest first.

    Days with no data are included, with zero counts, so the series has no
    gaps. Each item is ``{"date": ..., "pageviews": ..., "bot_hits": ...,
    "paths": {...}, "referrers": {...}, "languages": {...}, "devices": {...}}``.
    """
    today = today or datetime.now(ZoneInfo(timezone)).date().isoformat()
    last = date.fromisoformat(today)
    wanted = [(last - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    by_day = {d: {"date": d, **_empty_record()} for d in wanted}
    for key in store:
        parts = _split_key(key)
        if parts is None or parts[0] != app or parts[1] not in by_day:
            continue
        try:
            _merge(by_day[parts[1]], store[key])
        except KeyError:  # purged while we were reading
            continue
    return [by_day[d] for d in wanted]


def summarize(daily: list[dict]) -> dict:
    """Totals over a :func:`daily_counts` series: pageviews and each dimension."""
    total = _empty_record()
    for day in daily:
        _merge(total, day)

    def ranked(counts: dict) -> dict:
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    return {
        "pageviews": total["pageviews"],
        "bot_hits": total["bot_hits"],
        **{dim: ranked(total[dim]) for dim in DIMENSIONS},
    }


def analytics_report(
    app: str = "",
    *,
    days: int = 30,
    config: Optional["PlatformConfig"] = None,
    store: Optional[MutableMapping] = None,
) -> dict:
    """Per-app report for the last ``days`` days: daily series + totals.

    With no ``app``, reports every app that has data. ``config`` defaults to
    ``platform.toml`` in the current directory (only its ``[analytics]`` table
    is used; no app is imported).
    """
    if config is None:
        from enlace.base import PlatformConfig

        config = PlatformConfig.from_toml()
    settings = config.analytics
    if store is None:
        store = JsonFileStore(settings.store_path or default_store_path())
    names = [app] if app else apps_with_data(store)
    report = {}
    for name in names:
        series = daily_counts(store, name, days=days, timezone=settings.timezone)
        report[name] = {"days": series, "totals": summarize(series)}
    return report
