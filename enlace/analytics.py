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
  fragment dropped, and segments that look like an email, a UUID or a token
  replaced by ``:id`` (best effort — see :func:`normalize_path`);
- ``referrers``: the referring *host name* only, or ``(direct)`` /
  ``(internal)`` / ``(ip)``;
- ``languages``: the primary subtag of ``Accept-Language`` (``fr``, ``en``);
- ``devices``: ``mobile`` / ``tablet`` / ``desktop``;
- ``bot_hits``: page requests from crawlers and vulnerability scanners,
  counted apart and nowhere else.

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
records (``{app}/{day}/{writer}``), so workers never race on a record, and
readers sum the writers. Writes happen off the event loop, in a background
task started with the server; so does the daily maintenance, which purges
records past ``retention_days`` (whether or not anything was viewed that day)
and compacts each finished day's writer records into one.
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager, suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Optional, Sequence
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from enlace.base import AppConfig, PlatformConfig

_logger = logging.getLogger("enlace.analytics")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AnalyticsMode = Literal["none", "privacy"]

#: The CNIL's ceiling for keeping audience-measurement data is 25 months;
#: 750 days stays under it for any 25 consecutive months.
MAX_RETENTION_DAYS = 750

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
    Validated at config load, so ``enlace check`` catches a bad value before a
    boot does.
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
        description="Days of daily aggregates kept, today included; older ones "
        "are purged daily. At most 750 (under the CNIL's 25-month ceiling).",
    )
    timezone: str = Field(
        default="UTC", description="IANA zone whose midnight starts a new day."
    )
    flush_interval_seconds: float = Field(
        default=10.0,
        ge=0,
        description="How often each worker writes its buffered counts, in a "
        "background thread (0 = on every view, inline).",
    )
    max_values_per_dimension: int = Field(
        default=500,
        ge=1,
        description="Distinct values kept per dimension per day and worker; "
        "the rest are counted under '(other)'.",
    )
    honor_opt_out_signals: bool = Field(
        default=True, description="Skip requests carrying DNT: 1 or Sec-GPC: 1."
    )
    exclude_prefixes: tuple[str, ...] = Field(
        default=("/_", "/auth/"),
        description="Platform paths never attributed to any app.",
    )
    opt_out_cookie: str = "enlace_analytics_opt_out"

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        """Refuse an unknown zone here, not as a crash in every worker at boot."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value


def default_store_path() -> Path:
    """``$XDG_DATA_HOME/enlace/analytics``, else ``~/.local/share/enlace/analytics``."""
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "enlace" / "analytics"


# ---------------------------------------------------------------------------
# Throttled warnings: a broken disk must not also fill the journal
# ---------------------------------------------------------------------------

_WARN_EVERY_SECONDS = 3600


class _Throttle:
    """Log a warning (with traceback) at most once per key per interval."""

    def __init__(self, interval: float = _WARN_EVERY_SECONDS):
        self._interval = interval
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = defaultdict(int)

    def warning(self, key: str, msg: str, *args) -> None:
        now = time.monotonic()
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            self._suppressed[key] += 1
            return
        extra = self._suppressed.pop(key, 0)
        suffix = f" ({extra} similar suppressed)" if extra else ""
        self._last[key] = now
        _logger.warning(msg + suffix, *args, exc_info=True)


# ---------------------------------------------------------------------------
# Storage: the default store (any MutableMapping[str, dict] will do)
# ---------------------------------------------------------------------------

_KEY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.~%-]+$")
_TEMP_PREFIX = ".tmp-"
_STALE_TEMP_SECONDS = 3600


class JsonFileStore(MutableMapping):
    """``key -> dict``, one JSON file per key at ``{root}/{key}.json``.

    Keys are ``/``-separated relative paths of URL-safe segments. Writes are
    atomic (temp file + ``os.replace``), so a reader never sees half a record.
    Stdlib only; swap in any ``MutableMapping`` (e.g. a ``dol`` store over S3)
    through the ``store`` argument of :func:`make_analytics` or
    ``build_backend``. Two optional extras the maintenance uses when present:
    :meth:`exclusive` (a cross-process lock) and :meth:`sweep_temp_files`.
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
        fd, tmp = tempfile.mkstemp(
            dir=path.parent, prefix=_TEMP_PREFIX, suffix=self._SUFFIX
        )
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
        """Keys, lazily (so ``next(iter(store))`` does not walk the whole tree)."""
        if not self.root.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            rel = Path(dirpath).relative_to(self.root)
            for name in filenames:
                if name.endswith(self._SUFFIX) and not name.startswith("."):
                    yield (rel / name[: -len(self._SUFFIX)]).as_posix()

    def __len__(self) -> int:
        return sum(1 for _ in self)

    @contextmanager
    def exclusive(self) -> Iterator[bool]:
        """Try to take the store-wide maintenance lock; yield whether we got it.

        Non-blocking: a worker that loses the race skips this round of
        maintenance, it does not wait. ``False`` where ``fcntl`` is unavailable.
        """
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows
            yield False
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".maintenance.lock", "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def sweep_temp_files(self, *, older_than: float = _STALE_TEMP_SECONDS) -> int:
        """Delete temp files a killed write left behind; return how many."""
        if not self.root.is_dir():
            return 0
        cutoff = time.time() - older_than
        removed = 0
        for path in self.root.rglob(f"{_TEMP_PREFIX}*"):
            with suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
        return removed


#: The writer id of a finished day's compacted record.
MERGED = "merged"


def _app_segment(app: str) -> str:
    """An app name as one key segment (percent-encoded: ``café`` → ``caf%C3%A9``)."""
    return quote(app, safe="")


def _record_key(app: str, day: str, writer: str) -> str:
    return f"{_app_segment(app)}/{day}/{writer}"


def _split_key(key: str) -> Optional[tuple[str, str, str]]:
    """``(app, day, writer)`` from a record key, or ``None`` for anything else."""
    parts = key.split("/")
    if len(parts) != 3:
        return None
    return unquote(parts[0]), parts[1], parts[2]


# ---------------------------------------------------------------------------
# Classifying a request (pure functions)
# ---------------------------------------------------------------------------

OTHER = "(other)"
DIRECT = "(direct)"
INTERNAL = "(internal)"
IP = "(ip)"
UNKNOWN = "(unknown)"
REDACTED = ":id"

_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|scrap|curl|wget|python-|httpx|go-http|java/|"
    r"headless|lighthouse|pingdom|monitor|preview|facebookexternalhit|embedly",
    re.IGNORECASE,
)
_TABLET_RE = re.compile(
    r"ipad|tablet|kindle|silk|playbook|android(?!.*mobile)", re.IGNORECASE
)
_MOBILE_RE = re.compile(
    r"mobi|iphone|ipod|android|blackberry|opera mini|iemobile", re.IGNORECASE
)
_LANG_RE = re.compile(r"^[a-z]{2,3}$")
_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
_HOST_RE = re.compile(rf"^{_LABEL}(\.{_LABEL})*$")
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}"
)
_TOKEN_PIECE_RE = re.compile(r"(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9]{16,}")
_HEX_PIECE_RE = re.compile(r"[0-9a-fA-F]{16,}")
_PAGE_EXTENSIONS = (".html", ".htm")
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


def looks_like_probe(path: str) -> bool:
    """A scanner's request, not a page: a dot-segment or a non-HTML file name.

    An SPA answers ``/app/wp-admin/setup.php`` or ``/app/.env`` with its
    ``index.html``, so a browser-like scanner would otherwise pass as a reader.
    """
    segments = [s for s in path.split("/") if s]
    if any(s.startswith(".") for s in segments):
        return True
    last = segments[-1] if segments else ""
    return "." in last and not last.lower().endswith(_PAGE_EXTENSIONS)


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
    """The referring host name only.

    ``(direct)`` if none, ``(internal)`` if this site, ``(ip)`` for an address
    (it may be a person's own machine), ``(unknown)`` for anything that is not
    a plain host name.
    """
    if not referer:
        return DIRECT
    try:
        hostname = urlsplit(referer).hostname
    except ValueError:
        return UNKNOWN
    if not hostname:
        return UNKNOWN
    own = host.rsplit(":", 1)[0].lower() if host else ""
    if hostname == own:
        return INTERNAL
    with suppress(ValueError):
        ipaddress.ip_address(hostname)
        return IP
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return UNKNOWN
    return hostname if _HOST_RE.match(hostname) else UNKNOWN


def _redacted_segment(segment: str) -> str:
    """The segment, or ``:id`` if it looks like an identifier someone could own."""
    if "@" in segment or _UUID_RE.search(segment.lower()):
        return REDACTED
    for piece in re.split(r"[-_.~]", segment):
        if _TOKEN_PIECE_RE.fullmatch(piece) or _HEX_PIECE_RE.fullmatch(piece):
            return REDACTED
    return segment


def normalize_path(path: str) -> str:
    """A page path fit to store and to print.

    ``index.html`` folds into its directory; non-printable characters (terminal
    escapes) are dropped; segments that look like an email, a UUID or a long
    token become ``:id``; the result is capped at 200 characters. Redaction is
    best effort: an app that puts personal data in its URL paths should not
    turn analytics on.
    """
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    path = "".join(ch for ch in path if ch.isprintable())
    path = "/".join(_redacted_segment(s) if s else s for s in path.split("/"))
    return (path or "/")[:_MAX_PATH_LENGTH]


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

DIMENSIONS = ("paths", "referrers", "languages", "devices")

#: How often the background task wakes when nothing sets a shorter interval.
_IDLE_TICK_SECONDS = 60.0


def _empty_record() -> dict:
    return {"pageviews": 0, "bot_hits": 0, **{d: {} for d in DIMENSIONS}}


def _copy_record(rec: dict) -> dict:
    return {**rec, **{d: dict(rec[d]) for d in DIMENSIONS}}


def _merge(into: dict, rec: dict) -> None:
    into["pageviews"] += rec.get("pageviews", 0)
    into["bot_hits"] += rec.get("bot_hits", 0)
    for dim in DIMENSIONS:
        for value, n in (rec.get(dim) or {}).items():
            into[dim][value] = into[dim].get(value, 0) + n


class PageViewCounter:
    """Buffers one worker's daily aggregates and maintains the ``store``.

    Each worker writes only under its own ``writer`` id (derived from its
    process id at first use, so workers forked from one preloaded app still
    differ), overwriting its own cumulative record for the day: no worker ever
    read-modify-writes a record another is writing.

    Two schedules. :meth:`tick` — run by the server's background task, in a
    thread — flushes every ``flush_interval_seconds`` and runs :meth:`maintain`
    once per day. Without that task (``background=False``: a bare ASGI host, a
    test client outside ``with``), :meth:`record_view` flushes inline instead.
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
        self.flush_interval = flush_interval_seconds
        self._max_values = max_values_per_dimension
        self._fixed_writer = writer
        self._writer: Optional[tuple[int, str]] = None
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], dict] = {}
        self._dirty: set[tuple[str, str]] = set()
        self._last_flush = clock()
        self._maintained_on: Optional[str] = None
        self._warn = _Throttle()
        self.background = False

    @property
    def writer(self) -> str:
        """This process's writer id: fixed if given, else ``{pid}-{random}``."""
        if self._fixed_writer:
            return self._fixed_writer
        pid = os.getpid()
        if self._writer is None or self._writer[0] != pid:
            self._writer = (pid, f"{pid}-{uuid.uuid4().hex[:8]}")
        return self._writer[1]

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
        if not self.background or self.flush_interval == 0:
            self.maybe_flush()

    def record_bot_hit(self, app: str) -> None:
        """Count a request from a crawler or scanner, and nothing else about it."""
        with self._lock:
            self._record_for(app)["bot_hits"] += 1
        if not self.background or self.flush_interval == 0:
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
        if self._clock() - self._last_flush >= self.flush_interval:
            self.flush()

    def flush(self) -> None:
        """Write every changed record, and forget days that are over."""
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
                self._warn.warning(
                    "write", "analytics: could not write %s/%s", app, day
                )

    def tick(self) -> None:
        """One background round: flush when due, maintain once per day."""
        self.maybe_flush()
        today = self.today()
        if self._maintained_on != today:
            self.maintain(today=today)

    def maintain(self, *, today: Optional[str] = None) -> None:
        """Purge expired records; compact finished days; sweep stale temp files.

        Purging is idempotent, so every worker may do it. Compaction is not, so
        it runs only under the store's ``exclusive()`` lock, and only on days at
        least two days old, which no writer touches any more.
        """
        today = today or self.today()
        try:
            self.purge_expired(today=today)
            lock = getattr(self.store, "exclusive", None)
            if lock is not None:
                with lock() as got_it:
                    if got_it:
                        before = date.fromisoformat(today) - timedelta(days=1)
                        self.compact(before=before.isoformat())
                        sweep = getattr(self.store, "sweep_temp_files", None)
                        if sweep is not None:
                            sweep()
        except Exception:
            self._warn.warning("maintain", "analytics: daily maintenance failed")
            return
        self._maintained_on = today

    def purge_expired(self, *, today: Optional[str] = None) -> int:
        """Delete records older than the retention window; return how many.

        The window is ``retention_days`` days, today included.
        """
        today = today or self.today()
        first_kept = date.fromisoformat(today) - timedelta(days=self.retention_days - 1)
        cutoff = first_kept.isoformat()
        removed = 0
        for key in list(self.store):
            parts = _split_key(key)
            if parts is not None and parts[1] < cutoff:
                with suppress(KeyError):  # another worker got there first
                    del self.store[key]
                    removed += 1
        return removed

    def compact(self, *, before: str) -> int:
        """Fold each day's writer records (days before ``before``) into one.

        Every worker restart starts a new writer record, so a finished day can
        hold dozens of small files; this leaves one per app per day. Crash-safe:
        the merged record lists its ``sources``, so a source that survived an
        interrupted run is deleted without being counted twice. Call only under
        the store's exclusive lock. Returns how many records were folded.
        """
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for key in list(self.store):
            parts = _split_key(key)
            if parts is not None and parts[1] < before and parts[2] != MERGED:
                groups[(parts[0], parts[1])].append(key)
        folded = 0
        for (app, day), keys in groups.items():
            merged_key = _record_key(app, day, MERGED)
            try:
                merged = self.store[merged_key]
            except KeyError:
                merged = _empty_record()
            sources = set(merged.get("sources", []))
            merged = {**_empty_record(), **merged}
            for key in keys:
                if key in sources:
                    continue
                try:
                    _merge(merged, self.store[key])
                except KeyError:
                    continue
                sources.add(key)
                folded += 1
            merged["sources"] = sorted(sources)
            self.store[merged_key] = merged
            for key in keys:
                with suppress(KeyError):
                    del self.store[key]
        return folded

    async def run_background(self) -> None:
        """The server-lifetime loop: :meth:`tick` in a thread, forever."""
        interval = self.flush_interval or _IDLE_TICK_SECONDS
        self.background = True
        try:
            while True:
                await asyncio.to_thread(self.tick)
                await asyncio.sleep(interval)
        finally:
            self.background = False


# ---------------------------------------------------------------------------
# Attribution: which app does a path belong to?
# ---------------------------------------------------------------------------


class PageAttribution:
    """Maps a request path to ``(app, app-relative path)``, or ``None``.

    Platform paths (``exclude_prefixes``) never count. Otherwise the longest
    matching app mount (``/{name}/`` or its route prefix) wins, whether or not
    that app opted in — a page of an app that did not must never fall through
    to one that did. Anything else goes to the landing app, if it opted in.
    """

    def __init__(
        self,
        apps: Sequence["AppConfig"],
        *,
        landing_app: Optional[str] = None,
        exclude_prefixes: Sequence[str] = (),
    ):
        self.enabled = frozenset(a.name for a in apps if a.analytics.enabled)
        prefixes = {
            (prefix, a.name)
            for a in apps
            for prefix in (f"/{a.name}/", a.route_prefix.rstrip("/") + "/")
            if prefix != "/"
        }
        self._prefixes = sorted(prefixes, key=lambda p: -len(p[0]))
        self._excluded = tuple(exclude_prefixes)
        self._landing = landing_app if landing_app in self.enabled else None
        # An app mounted at "/" (route = "/") is the fallback, like a landing app.
        for a in apps:
            if a.route_prefix.rstrip("/") == "" and a.name in self.enabled:
                self._landing = self._landing or a.name

    def __call__(self, path: str) -> Optional[tuple[str, str]]:
        if path.startswith(self._excluded):
            return None
        for prefix, name in self._prefixes:
            if path.startswith(prefix):
                if name not in self.enabled:
                    return None
                return name, "/" + path[len(prefix) :]
        if self._landing:
            return self._landing, path
        return None


# ---------------------------------------------------------------------------
# Collecting: the middleware
# ---------------------------------------------------------------------------


def _headers(raw: Sequence[tuple[bytes, bytes]]) -> dict[str, str]:
    """Lowercased header dict; repeated headers joined (cookies with ``; ``)."""
    out: dict[str, str] = {}
    for k, v in raw:
        name, value = k.decode("latin-1").lower(), v.decode("latin-1")
        if name in out:
            out[name] += ("; " if name == "cookie" else ", ") + value
        else:
            out[name] = value
    return out


class PageViewMiddleware:
    """Pure-ASGI middleware counting page views of the apps that opted in.

    It only observes: the request and response pass through unchanged, and
    nothing is added to the page. It also owns the counter's lifetime: the
    background flush/maintenance task starts with the server's lifespan and
    the last flush happens at shutdown.
    """

    def __init__(
        self,
        app,
        *,
        counter: PageViewCounter,
        attribute: Callable[[str], Optional[tuple[str, str]]],
        settings: Optional[PlatformAnalyticsConfig] = None,
    ):
        self.app = app
        self._counter = counter
        self._attribute = attribute
        self._settings = settings or PlatformAnalyticsConfig()
        self._task: Optional[asyncio.Task] = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, self._lifespan_receive(receive), send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        target = self._attribute(scope.get("path", ""))
        headers = _headers(scope.get("headers", [])) if target else {}
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
            if not is_page_response(message["status"], _headers(message["headers"])):
                return
            app, path = target
            device = device_class(
                headers.get("user-agent", ""),
                client_hint_mobile=headers.get("sec-ch-ua-mobile", ""),
            )
            if device == "bot" or looks_like_probe(path):
                self._counter.record_bot_hit(app)
                return
            self._counter.record_view(
                app,
                path=normalize_path(path),
                referrer=referrer_domain(
                    headers.get("referer", ""), host=headers.get("host", "")
                ),
                language=primary_language(headers.get("accept-language", "")),
                device=device,
            )
        except Exception:  # counting must never break a page
            _logger.warning("analytics: failed to count a page view", exc_info=True)

    def _lifespan_receive(self, receive):
        async def wrapped():
            message = await receive()
            kind = message.get("type")
            if kind == "lifespan.startup" and self._task is None:
                self._task = asyncio.get_running_loop().create_task(
                    self._counter.run_background()
                )
            elif kind == "lifespan.shutdown":
                await self._stop()
            return message

        return wrapped

    async def _stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        try:
            await asyncio.to_thread(self._counter.flush)
        except Exception:
            _logger.warning("analytics: final flush failed", exc_info=True)


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
_OPT_OUT_MAX_AGE = 13 * 30 * 24 * 3600


def _add_opt_out_route(parent: "FastAPI", settings: PlatformAnalyticsConfig) -> None:
    """``GET /_analytics/opt-out[?choice=out|in]``: a page a privacy notice links to.

    A plain page with one link, no script. Choosing "out" sets one first-party
    cookie (exempt from consent: it stores the visitor's refusal); "in" removes
    it. GET, so it works as a plain link and needs no CSRF token. The flip side:
    a forged link can switch someone's choice either way; it can reveal nothing,
    since the page shows only the visitor's own choice back to them.
    """
    import html

    from fastapi import Request
    from fastapi.responses import HTMLResponse

    cookie = settings.opt_out_cookie
    base = f"{ANALYTICS_ROUTE_PREFIX}/opt-out"

    @parent.get(base, include_in_schema=False)
    async def opt_out_page(request: Request, choice: str = "") -> HTMLResponse:
        lang = primary_language(request.headers.get("accept-language", ""))
        lang = lang if lang in _OPT_OUT_TEXT else "en"
        text = {k: html.escape(v) for k, v in _OPT_OUT_TEXT[lang].items()}
        opted_out = request.cookies.get(cookie) == "1"
        if choice in ("out", "in"):
            opted_out = choice == "out"
        body = (
            f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{text['title']}</title></head>"
            '<body style="font-family:system-ui,sans-serif;max-width:36rem;'
            'margin:3rem auto;padding:0 1rem;line-height:1.5">'
            f"<h1>{text['title']}</h1><p>{text['about']}</p>"
            f"<p><strong>{text['out'] if opted_out else text['in']}</strong></p>"
            f'<p><a href="{base}?choice={"in" if opted_out else "out"}">'
            f"{text['do_in'] if opted_out else text['do_out']}</a></p></body></html>"
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
    """The analytics feature for one platform: counter, attribution, routes.

    ``collecting`` is False when no app opted in but old data exists: then only
    the daily maintenance runs, so retention keeps being enforced after every
    app has turned analytics off.
    """

    def __init__(
        self,
        config: "PlatformConfig",
        counter: PageViewCounter,
        *,
        collecting: bool = True,
    ):
        self.config = config
        self.settings = config.analytics
        self.counter = counter
        self.collecting = collecting
        self.attribute = PageAttribution(
            config.apps if collecting else (),
            landing_app=config.landing_app,
            exclude_prefixes=self.settings.exclude_prefixes,
        )

    def add_routes(self, parent: "FastAPI") -> None:
        """Register the opt-out page. Call before any catch-all ``/`` mount."""
        parent.state.analytics = self
        if self.collecting:
            _add_opt_out_route(parent, self.settings)

    def add_middleware(self, parent: "FastAPI") -> None:
        """Install the counting (and lifespan-owning) middleware."""
        parent.add_middleware(
            PageViewMiddleware,
            counter=self.counter,
            attribute=self.attribute,
            settings=self.settings,
        )


def _has_data(store: MutableMapping) -> bool:
    try:
        return next(iter(store), None) is not None
    except Exception:
        return False


def make_analytics(
    config: "PlatformConfig", *, store: Optional[MutableMapping] = None
) -> Optional[Analytics]:
    """The platform's analytics, or ``None`` when there is nothing to do.

    Nothing to do means no app opted in *and* the store holds no data. If data
    remains after every app opted out, maintenance alone still runs, so it is
    purged on schedule. ``store`` is the storage seam: any
    ``MutableMapping[str, dict]``. Default: a :class:`JsonFileStore` at
    ``[analytics].store_path`` (or :func:`default_store_path`).
    """
    settings = config.analytics
    if store is None:
        store = JsonFileStore(settings.store_path or default_store_path())
    collecting = any(a.analytics.enabled for a in config.apps)
    if not collecting and not _has_data(store):
        return None
    counter = PageViewCounter(
        store,
        retention_days=settings.retention_days,
        timezone=settings.timezone,
        flush_interval_seconds=settings.flush_interval_seconds,
        max_values_per_dimension=settings.max_values_per_dimension,
    )
    return Analytics(config, counter, collecting=collecting)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _index(store: MutableMapping) -> dict[str, dict[str, list[str]]]:
    """``{app: {day: [keys to sum]}}`` from ONE pass over the store's keys.

    A day's merged record replaces the writer records it lists as ``sources``,
    so a reader running during compaction never counts a record twice.
    """
    index: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for key in store:
        parts = _split_key(key)
        if parts is not None:
            index[parts[0]][parts[1]].append(key)
    for days in index.values():
        for day, keys in days.items():
            merged = [k for k in keys if k.rsplit("/", 1)[-1] == MERGED]
            if merged:
                with suppress(KeyError):
                    sources = set(store[merged[0]].get("sources", []))
                    days[day] = [k for k in keys if k not in sources]
    return index


def apps_with_data(store: MutableMapping) -> list[str]:
    """Names of the apps that have any analytics records."""
    return sorted(_index(store))


def daily_counts(
    store: MutableMapping,
    app: str,
    *,
    days: int = 30,
    today: Optional[str] = None,
    timezone: str = "UTC",
    index: Optional[dict] = None,
) -> list[dict]:
    """One merged record per day for the last ``days`` days, oldest first.

    Days with no data are included, with zero counts, so the series has no
    gaps. Each item is ``{"date": ..., "pageviews": ..., "bot_hits": ...,
    "paths": {...}, "referrers": {...}, "languages": {...}, "devices": {...}}``.
    Pass ``index`` (from one scan) when reading several apps.
    """
    today = today or datetime.now(ZoneInfo(timezone)).date().isoformat()
    last = date.fromisoformat(today)
    wanted = [(last - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    app_days = (index if index is not None else _index(store)).get(app, {})
    series = []
    for day in wanted:
        record = {"date": day, **_empty_record()}
        for key in app_days.get(day, ()):
            with suppress(KeyError):  # purged while we were reading
                _merge(record, store[key])
        series.append(record)
    return series


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


def default_analytics_store(config: Optional["PlatformConfig"] = None) -> JsonFileStore:
    """The default store a platform config points at."""
    settings = config.analytics if config is not None else PlatformAnalyticsConfig()
    return JsonFileStore(settings.store_path or default_store_path())


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
    if store is None:
        store = default_analytics_store(config)
    index = _index(store)
    names = [app] if app else sorted(index)
    report = {}
    for name in names:
        series = daily_counts(
            store, name, days=days, timezone=config.analytics.timezone, index=index
        )
        report[name] = {"days": series, "totals": summarize(series)}
    return report
