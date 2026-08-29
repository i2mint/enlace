"""Post-deploy smoke checks for a running enlace gateway.

Complements ``enlace check`` (static config validation) by probing a live
gateway over HTTP. Catches silent-degradation failures that static analysis
can't — the incident that motivated this (i2mint/enlace#11) was a gateway
booting cleanly with auth un-mounted because the signing key was missing at
startup; auth-specific checks for that scenario live in
``enlace_auth.diagnostics``.

Design:
- Pure stdlib ``urllib`` for HTTP. No new deps; this must work in minimal
  deploy venvs.
- Every check is a ``Check(name, status, detail)``. The runner collects all
  of them and returns a ``Report`` so callers can emit pretty text OR JSON.
- ``detail`` is a short human-readable string. Structured payloads go in
  ``extra`` (dict) so ``--json`` consumers don't re-parse prose.
- An app enlace could not even import is reported as a ``FAIL`` check, not as
  a traceback — see ``_check_app_imports`` and ``discover_apps``'
  ``on_import_error``. A health tool that dies on the failure it exists to
  detect reports nothing about the thirty apps that are fine.
- Plugins can supply extra static or HTTP probes via ``extra_static_checks``
  and ``extra_http_checks`` on ``run_doctor``. Those are also discovered
  automatically from the plugins the platform is configured to load — see
  :func:`discover_plugin_checks`. A check nobody runs is not a check, and the
  hand-wiring alternative meant a plugin could ship a correct diagnosis of its
  own failure mode that never once executed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from enlace.base import PlatformConfig

_DEFAULT_TIMEOUT = 5.0

PASS = "pass"
FAIL = "fail"
WARN = "warn"
SKIP = "skip"


@dataclass
class Check:
    """Result of a single probe."""

    name: str
    status: str  # "pass" | "fail" | "warn" | "skip"
    detail: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Report:
    """Aggregate report for one run of the doctor."""

    base_url: Optional[str]
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff no check failed. Warnings don't flip the result."""
        return not any(c.status == FAIL for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "ok": self.ok,
            "summary": self._summary(),
            "checks": [asdict(c) for c in self.checks],
        }

    def _summary(self) -> dict:
        counts = {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0}
        for c in self.checks:
            counts[c.status] = counts.get(c.status, 0) + 1
        return counts

    def format_text(self) -> str:
        """Pretty-print as a human-readable report."""
        symbol = {PASS: "OK  ", FAIL: "FAIL", WARN: "WARN", SKIP: "skip"}
        lines = []
        if self.base_url:
            lines.append(f"enlace doctor — probing {self.base_url}")
        else:
            lines.append("enlace doctor — static checks only")
        lines.append("=" * 60)
        for c in self.checks:
            prefix = symbol.get(c.status, "??  ")
            line = f"  [{prefix}] {c.name}"
            if c.detail:
                line += f" — {c.detail}"
            lines.append(line)
        summary = self._summary()
        lines.append("")
        lines.append(
            f"Result: {summary[PASS]} pass, {summary[FAIL]} fail, "
            f"{summary[WARN]} warn, {summary[SKIP]} skip"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Static checks (run without a live gateway)
# ---------------------------------------------------------------------------


def _check_app_imports(config: PlatformConfig) -> list[Check]:
    """Apps whose entry module could not be imported at discovery time.

    Only ever non-empty when the caller discovered with
    ``on_import_error="record"`` (the CLI's diagnostic verbs do) — under the
    default ``"raise"`` policy discovery dies before there is anything to
    report, which is the very failure this check exists to replace.
    """
    out: list[Check] = []
    for app in config.apps:
        err = app.import_error
        if err is None:
            continue
        out.append(
            Check(
                f"import:{app.name}",
                FAIL,
                f"{err.exception_type}: {err.message}",
                extra={
                    "app": app.name,
                    "exception_type": err.exception_type,
                    "message": err.message,
                    "entry_module_path": str(err.entry_module_path or ""),
                },
            )
        )
    return out


def _check_frontend_dirs(config: PlatformConfig) -> list[Check]:
    """Apps declaring frontend_dir should actually have a directory there."""
    out: list[Check] = []
    for app in config.apps:
        if not app.frontend_dir:
            continue
        if not app.frontend_dir.exists():
            out.append(
                Check(
                    f"frontend_dir:{app.name}",
                    WARN,
                    f"{app.frontend_dir} does not exist; frontend will not mount",
                )
            )
        elif not app.frontend_dir.is_dir():
            out.append(
                Check(
                    f"frontend_dir:{app.name}",
                    FAIL,
                    f"{app.frontend_dir} exists but is not a directory",
                )
            )
    return out


# ---------------------------------------------------------------------------
# HTTP probes
# ---------------------------------------------------------------------------


def _http_get(
    url: str, *, timeout: float = _DEFAULT_TIMEOUT
) -> tuple[Optional[int], dict[str, str], Optional[bytes], Optional[str]]:
    """GET a URL. Returns (status, headers, body, error_message)."""
    req = Request(url, method="GET", headers={"User-Agent": "enlace-doctor/1"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, headers, body, None
    except HTTPError as e:  # 4xx/5xx still come here
        headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        body = e.read() if hasattr(e, "read") else None
        return e.code, headers, body, None
    except URLError as e:
        return None, {}, None, f"connection failed: {e.reason}"
    except Exception as e:  # pragma: no cover - defensive
        return None, {}, None, f"unexpected error: {e}"


def _check_frontend_mount(base_url: str, app_name: str, timeout: float) -> Check:
    """GET /{app_name}/ — mount must exist.

    Accepts 200/text-html (public app served), 401 (protected app, auth
    middleware is doing its job), and 3xx (redirect to login). The mount
    existing is what we care about; authentication state is out of scope
    for a post-deploy smoke.

    Fails on 404 (mount missing — the SPA catch-all or a hole), 5xx, and
    connection errors.
    """
    url = f"{base_url.rstrip('/')}/{app_name}/"
    status, headers, _body, err = _http_get(url, timeout=timeout)
    name = f"http:/{app_name}/"
    if err:
        return Check(name, FAIL, err)
    if status == 200:
        ct = headers.get("content-type", "")
        if "text/html" not in ct.lower():
            return Check(name, WARN, f"status=200 but content-type={ct!r}")
        return Check(name, PASS, f"200 OK ({ct})")
    if status == 401:
        return Check(name, PASS, "status=401 (protected; auth middleware active)")
    if status in (301, 302, 303, 307, 308):
        location = headers.get("location", "")
        return Check(name, PASS, f"status={status} redirect to {location!r}")
    if status == 404:
        return Check(name, FAIL, "status=404 (mount missing?)")
    if status is not None and status >= 500:
        return Check(name, FAIL, f"status={status} (5xx)")
    return Check(name, WARN, f"status={status} (unexpected but not fatal)")


def _check_api_mount(
    base_url: str, app_name: str, route_prefix: str, timeout: float
) -> Check:
    """GET the API prefix root. Non-5xx is considered healthy.

    Apps vary: some mount /docs, others return a 404 at the bare prefix but
    work fine, others redirect. We only fail on 5xx or connection errors —
    the point is to detect "something ate my traffic", not to enforce API
    conventions each app chose not to follow.
    """
    url = f"{base_url.rstrip('/')}{route_prefix.rstrip('/')}/"
    status, _headers, _body, err = _http_get(url, timeout=timeout)
    name = f"http:{route_prefix}/"
    if err:
        return Check(name, FAIL, err)
    if status is not None and status >= 500:
        return Check(name, FAIL, f"status={status} (5xx)")
    return Check(name, PASS, f"status={status}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def discover_plugin_checks(
    specs: Optional[str] = None,
) -> "tuple[tuple[StaticCheckFn, ...], tuple[HttpCheckFn, ...]]":
    """Collect doctor checks contributed by the configured enlace plugins.

    A plugin package may expose a ``diagnostics`` submodule with
    ``static_checks`` and/or ``http_checks`` tuples; ``enlace_auth`` is the
    reference. Plugins come from *specs* or, by default, the same
    ``ENLACE_PLUGINS`` environment variable that decides what the platform
    actually loads — so the checks that run always match the code that runs.

    Discovery never raises: a plugin without a ``diagnostics`` module, or one
    that cannot be imported, contributes nothing. The doctor's job is to report
    problems, not to become one.

    >>> static, http = discover_plugin_checks("")
    >>> static, http
    ((), ())
    """
    import importlib
    import os

    raw = os.environ.get("ENLACE_PLUGINS", "") if specs is None else specs
    static: list[StaticCheckFn] = []
    http: list[HttpCheckFn] = []
    seen: set[str] = set()
    for spec in (s.strip() for s in (raw or "").split(",")):
        if not spec:
            continue
        package = spec.split(":", 1)[0].split(".", 1)[0]
        if package in seen:
            continue
        seen.add(package)
        try:
            diagnostics = importlib.import_module(f"{package}.diagnostics")
        except Exception:  # noqa: BLE001 - a broken plugin must not break doctor
            continue
        static.extend(getattr(diagnostics, "static_checks", ()) or ())
        http.extend(getattr(diagnostics, "http_checks", ()) or ())
    return tuple(static), tuple(http)


StaticCheckFn = Callable[[PlatformConfig], "Iterable[Check]"]
HttpCheckFn = Callable[[PlatformConfig, str, float], "Iterable[Check]"]


def run_doctor(
    config: PlatformConfig,
    *,
    base_url: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    app_filter: Optional[Iterable[str]] = None,
    include_env_checks: bool = True,
    extra_static_checks: Iterable[StaticCheckFn] = (),
    extra_http_checks: Iterable[HttpCheckFn] = (),
) -> Report:
    """Run all checks and return a ``Report``.

    Args:
        config: Platform config to check against.
        base_url: When set, HTTP probes are run against this URL. When
            unset, only static checks run.
        timeout: Per-request timeout for HTTP probes.
        app_filter: If given, only probe these app names (static checks
            still run across all apps).
        include_env_checks: Forwarded to ``extra_static_checks`` callbacks
            via ``config`` (those that read env vars should respect their
            caller's intent — see ``enlace_auth.diagnostics`` for the
            convention). enlace itself has no env-var checks of its own.
        extra_static_checks: Plugin-provided static checks. Each is a
            callable that receives ``config`` and returns ``Iterable[Check]``.
        extra_http_checks: Plugin-provided HTTP checks. Each is a callable
            that receives ``(config, base_url, timeout)`` and returns
            ``Iterable[Check]``. Only invoked when ``base_url`` is set.
    """
    _ = include_env_checks  # signal to plugin authors via convention
    report = Report(base_url=base_url)

    report.checks.extend(_check_app_imports(config))
    report.checks.extend(_check_frontend_dirs(config))
    for fn in extra_static_checks:
        report.checks.extend(fn(config))

    # HTTP probes — only when a base URL is provided.
    if base_url:
        for fn in extra_http_checks:
            report.checks.extend(fn(config, base_url, timeout))

        apps = list(config.apps)
        if app_filter is not None:
            wanted = set(app_filter)
            apps = [a for a in apps if a.name in wanted]

        for app in apps:
            has_frontend = bool(app.frontend_dir and app.frontend_dir.is_dir()) or (
                app.mode == "static"
            )
            if has_frontend:
                report.checks.append(_check_frontend_mount(base_url, app.name, timeout))
            if app.app_type != "frontend_only" and app.mode != "static":
                report.checks.append(
                    _check_api_mount(base_url, app.name, app.route_prefix, timeout)
                )

    return report


def _format_as_json(report: Report) -> str:
    return json.dumps(report.as_dict(), indent=2, default=str)


__all__ = [
    "Check",
    "Report",
    "run_doctor",
    "PASS",
    "FAIL",
    "WARN",
    "SKIP",
]
