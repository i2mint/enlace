"""Loud-and-local app-load failures: a 500 stub for the one app that broke.

An ``asgi``-mode app is mounted by *importing* it, in the gateway's own process.
Before this module, one app whose import raised took every worker down with it,
and every app on the platform answered 502. Now :func:`load_or_stub` contains
that failure to the app: the app's route prefix answers 500 naming the app and
the exception class, and the other apps serve.

A stub is **not** a degraded success. It is as loud as the crash-loop it
replaces, only not as wide. Every stub is reported in four places, all fed by
the same ``AppConfig.import_error`` record:

- the logs: one ERROR line with the traceback per stubbed app, at boot;
- ``/_meta``: the platform payload gains ``degraded_apps``, and the app's own
  ``/_meta`` gains ``degraded`` (see :func:`degraded_apps`);
- ``/_apps``: the app is listed with ``degraded: true`` and is not launchable;
- ``enlace doctor``: a FAIL check per stubbed app, read from the live
  gateway's ``/_meta``, so a failure only the server's own identity hits (a
  file only it cannot read) is still reported.

What is contained is narrow on purpose: an ``asgi``-mode app's import, or the
detection and loading of its sub-app. Platform configuration errors (a bad
``platform.toml``, an unknown ``landing_app``, route conflicts, a plugin that
refuses its configuration) still refuse to boot, as do lifespan failures, an
app with ``fail_hard = true`` in its app.toml, and every app when the platform
sets ``app_load_failure = "raise"``. The decision and its reasons are recorded
in ``misc/docs/decisions/0001-loud-and-local-app-load-failures.md``.
"""

import logging
from typing import TYPE_CHECKING, Any, Optional

from enlace.base import CONTAINED_LOAD_ERRORS, AppConfig, AppImportError, PlatformConfig

if TYPE_CHECKING:
    from enlace.strategies import BackendStrategy

_logger = logging.getLogger("enlace")

#: Status the stub answers every HTTP request with.
STUB_STATUS = 500

#: Response header naming the condition, so a log line or a probe can tell a
#: stub's 500 from an app's own.
STUB_HEADER = "x-enlace-app-status"
STUB_HEADER_VALUE = "load-failed"

#: Close code for a websocket handshake to a stub: "internal error".
_WS_INTERNAL_ERROR = 1011

#: Platform ``/_meta`` key listing stubbed apps. Present only when one is, so a
#: healthy ``/_meta`` stays quiet (the same convention as ``manifest_error``).
DEGRADED_APPS_KEY = "degraded_apps"

#: Per-app ``/_meta`` and ``/_apps`` key flagging a stubbed app.
DEGRADED_KEY = "degraded"

#: Modes whose load failures are contained. Only ``asgi`` imports app code
#: into the gateway; every other mode's ``make_asgi`` failure is configuration.
CONTAINED_MODES = frozenset({"asgi"})


class EnlaceAppLoadError(RuntimeError):
    """An app could not be loaded and its configuration says not to stub it.

    Raised only when the original exception is not available to re-raise
    (an ``AppImportError`` built by hand rather than from an exception).
    """


class UnavailableApp:
    """Pure-ASGI stub standing in for an app that failed to load.

    Every HTTP request answers 500 with a short plain-text body naming the app
    and the exception *class* — never the message or a traceback, which can
    carry server paths and is logged instead. A websocket handshake is refused
    (a close before accept: uvicorn answers it as an HTTP 403; the ASGI close
    code is 1011). It has no ``router``, so the gateway's lifespan cascade
    passes it by.
    """

    def __init__(self, app_name: str, exception_type: str):
        self.app_name = app_name
        self.exception_type = exception_type
        self._body = (
            f"enlace: app {app_name!r} failed to load ({exception_type}) and is "
            "unavailable. The platform's other apps are not affected; the "
            "server log has the details.\n"
        ).encode()

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": STUB_STATUS,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(self._body)).encode()),
                        (b"cache-control", b"no-store"),
                        (STUB_HEADER.encode(), STUB_HEADER_VALUE.encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": self._body})
        elif kind == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": _WS_INTERNAL_ERROR})


def load_or_stub(
    strategy: "BackendStrategy", app: AppConfig, platform: PlatformConfig
) -> Optional[Any]:
    """Return ``app``'s ASGI sub-app, or a stub when an asgi app cannot load.

    An app that discovery already recorded as un-importable is not imported a
    second time. A contained compose-time failure is recorded on ``app``
    (mutated in place, like ``updated_at``) so every reporting surface reads
    the same record. A failure that must not be contained is re-raised with its
    original traceback.
    """
    if app.import_error is None:
        try:
            return strategy.make_asgi(app, platform)
        except CONTAINED_LOAD_ERRORS as exc:
            if app.mode not in CONTAINED_MODES or _must_refuse_to_boot(app, platform):
                raise
            app.import_error = AppImportError.from_exception(
                exc, app.entry_module_path, stage="compose"
            )
    elif _must_refuse_to_boot(app, platform):
        _reraise(app.import_error, app.name)

    _log_stub(app)
    return UnavailableApp(app.name, app.import_error.exception_type)


def degraded_apps(config: PlatformConfig) -> dict[str, dict[str, str]]:
    """The apps that failed to load, by name, with their public summary.

    >>> degraded_apps(PlatformConfig())
    {}
    """
    return {
        app.name: app.import_error.public_summary()
        for app in config.apps
        if app.import_error is not None
    }


def refuse_to_boot_if_required(platform: PlatformConfig) -> None:
    """Raise the first recorded load failure the configuration says not to stub.

    For callers that act on a discovered config *before* ``build_backend`` runs
    — ``enlace serve`` spawns process-mode children and a supervised gateway —
    so ``fail_hard`` and ``app_load_failure = "raise"`` still stop everything
    up front, as they did when discovery raised.
    """
    for app in platform.apps:
        if app.import_error is not None and _must_refuse_to_boot(app, platform):
            _reraise(app.import_error, app.name)


def _must_refuse_to_boot(app: AppConfig, platform: PlatformConfig) -> bool:
    """Whether this app's load failure must stop the gateway instead."""
    return app.fail_hard or platform.app_load_failure == "raise"


def _reraise(record: AppImportError, app_name: str) -> None:
    """Raise the recorded failure, with its original traceback when there is one."""
    if record.exception is not None:
        raise record.exception
    raise EnlaceAppLoadError(f"app {app_name!r} failed to load: {record}")


def _log_stub(app: AppConfig) -> None:
    """One ERROR line per stubbed app, with the traceback when there is one."""
    record = app.import_error
    exc = record.exception
    _logger.error(
        "enlace: app %r failed to load at %s (%s); serving a %d stub at %s. "
        "Every other app is unaffected. Fix the app and restart; set "
        "fail_hard = true in its app.toml to refuse to boot instead.",
        app.name,
        record.stage,
        record,
        STUB_STATUS,
        app.route_prefix,
        exc_info=(type(exc), exc, exc.__traceback__) if exc is not None else None,
    )
