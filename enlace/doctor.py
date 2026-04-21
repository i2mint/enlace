"""Post-deploy smoke checks for a running enlace gateway.

Complements ``enlace check`` (static config validation) by probing a live
gateway over HTTP. Catches silent-degradation failures that static analysis
can't — the incident that motivated this (i2mint/enlace#11) was a gateway
booting cleanly with ``/auth/*`` un-mounted because ``ENLACE_SIGNING_KEY``
was missing at startup.

Design:
- Pure stdlib ``urllib`` for HTTP. No new deps; this must work in minimal
  deploy venvs.
- Every check is a ``Check(name, status, detail)``. The runner collects all
  of them and returns a ``Report`` so callers can emit pretty text OR JSON.
- ``detail`` is a short human-readable string. Structured payloads go in
  ``extra`` (dict) so ``--json`` consumers don't re-parse prose.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional
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


def _check_signing_key(config: PlatformConfig) -> Check:
    auth = config.auth
    if not auth.enabled:
        return Check("signing_key", SKIP, "auth.enabled=false")
    raw = os.environ.get(auth.signing_key_env) or ""
    stripped = raw.strip()
    if not stripped:
        return Check(
            "signing_key",
            FAIL,
            f"env var {auth.signing_key_env} is unset or empty",
        )
    if len(stripped) < 32:
        return Check(
            "signing_key",
            FAIL,
            f"env var {auth.signing_key_env} too short ({len(stripped)} chars)",
        )
    return Check(
        "signing_key",
        PASS,
        f"{auth.signing_key_env} set ({len(stripped)} chars)",
    )


def _check_shared_passwords(config: PlatformConfig) -> list[Check]:
    if not config.auth.enabled:
        return []
    out: list[Check] = []
    for app in config.apps:
        if app.access != "protected:shared":
            continue
        if not app.shared_password_env:
            out.append(
                Check(
                    f"shared_pw:{app.name}",
                    FAIL,
                    "access=protected:shared but no shared_password_env set",
                )
            )
            continue
        if not os.environ.get(app.shared_password_env):
            out.append(
                Check(
                    f"shared_pw:{app.name}",
                    FAIL,
                    f"env var {app.shared_password_env} is unset",
                )
            )
        else:
            out.append(
                Check(
                    f"shared_pw:{app.name}",
                    PASS,
                    f"{app.shared_password_env} set",
                )
            )
    return out


def _check_oauth_importable(config: PlatformConfig) -> Optional[Check]:
    """If OAuth providers are configured, authlib must be importable."""
    if not config.auth.enabled or not config.auth.oauth:
        return None
    providers = ", ".join(sorted(config.auth.oauth))
    try:
        import authlib  # noqa: F401
    except ImportError:
        return Check(
            "oauth_import",
            FAIL,
            f"oauth providers ({providers}) configured but authlib not "
            "installed. Install with `pip install enlace[oauth]`.",
        )
    return Check("oauth_import", PASS, f"authlib importable; providers: {providers}")


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


def _check_csrf(base_url: str, timeout: float) -> Check:
    """GET /auth/csrf must return JSON with a 'csrf' key.

    This is THE check that would have caught the i2mint/enlace#11 regression:
    when auth is silently disabled, the SPA catch-all returns
    ``<!doctype html>`` instead of JSON.
    """
    url = f"{base_url.rstrip('/')}/auth/csrf"
    status, headers, body, err = _http_get(url, timeout=timeout)
    if err:
        return Check("http:/auth/csrf", FAIL, err)
    ct = headers.get("content-type", "")
    if status != 200:
        snippet = (body or b"").decode("utf-8", errors="replace")[:120]
        return Check(
            "http:/auth/csrf",
            FAIL,
            f"status={status} content-type={ct!r} body[:120]={snippet!r}",
            extra={"status": status, "content_type": ct},
        )
    if "application/json" not in ct.lower():
        snippet = (body or b"").decode("utf-8", errors="replace")[:120]
        return Check(
            "http:/auth/csrf",
            FAIL,
            f"expected JSON, got content-type={ct!r}; "
            f"body[:120]={snippet!r} (auth silently disabled?)",
            extra={"status": status, "content_type": ct},
        )
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception as e:
        return Check(
            "http:/auth/csrf",
            FAIL,
            f"body is not valid JSON: {e}",
        )
    if not isinstance(data, dict) or "csrf" not in data:
        keys = list(data) if isinstance(data, dict) else type(data).__name__
        return Check(
            "http:/auth/csrf",
            FAIL,
            f"JSON response missing 'csrf' key: keys={keys}",
        )
    return Check(
        "http:/auth/csrf", PASS, f"JSON with csrf token ({len(data['csrf'])} chars)"
    )


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


def run_doctor(
    config: PlatformConfig,
    *,
    base_url: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    app_filter: Optional[Iterable[str]] = None,
    include_env_checks: bool = True,
) -> Report:
    """Run all checks and return a ``Report``.

    Args:
        config: Platform config to check against.
        base_url: When set, HTTP probes are run against this URL. When
            unset, only static checks run.
        timeout: Per-request timeout for HTTP probes.
        app_filter: If given, only probe these app names (static checks
            still run across all apps).
        include_env_checks: When False, env-var checks (signing key,
            shared-password hashes) are skipped. Useful when probing from
            a shell that doesn't have the gateway's env loaded — the HTTP
            probes then serve as the source of truth. Callers that have
            loaded the env (e.g. ``--envfile``) should leave this True.
    """
    report = Report(base_url=base_url)

    # Static checks that depend on the local environment.
    if include_env_checks:
        report.checks.append(_check_signing_key(config))
        report.checks.extend(_check_shared_passwords(config))
    # Static checks that only depend on the repo / config — always run.
    oauth_check = _check_oauth_importable(config)
    if oauth_check is not None:
        report.checks.append(oauth_check)
    report.checks.extend(_check_frontend_dirs(config))

    # HTTP probes — only when a base URL is provided.
    if base_url:
        if config.auth.enabled:
            report.checks.append(_check_csrf(base_url, timeout))
        else:
            report.checks.append(Check("http:/auth/csrf", SKIP, "auth.enabled=false"))

        apps = list(config.apps)
        if app_filter is not None:
            wanted = set(app_filter)
            apps = [a for a in apps if a.name in wanted]

        for app in apps:
            has_frontend = bool(app.frontend_dir and app.frontend_dir.is_dir()) or (
                app.mode == "static"
            )
            if has_frontend:
                report.checks.append(
                    _check_frontend_mount(base_url, app.name, timeout)
                )
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
