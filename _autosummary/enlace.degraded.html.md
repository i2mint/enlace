# enlace.degraded

Loud-and-local app-load failures: a 500 stub for the one app that broke.

An `asgi`-mode app is mounted by *importing* it, in the gateway’s own process.
Before this module, one app whose import raised took every worker down with it,
and every app on the platform answered 502. Now [`load_or_stub()`](#enlace.degraded.load_or_stub) contains
that failure to the app: the app’s route prefix answers 500 naming the app and
the exception class, and the other apps serve.

A stub is **not** a degraded success. It is as loud as the crash-loop it
replaces, only not as wide. Every stub is reported in four places, all fed by
the same `AppConfig.import_error` record:

- the logs: one ERROR line with the traceback per stubbed app, at boot;
- `/_meta`: the platform payload gains `degraded_apps`, and the app’s own
  `/_meta` gains `degraded` (see [`degraded_apps()`](#enlace.degraded.degraded_apps));
- `/_apps`: the app is listed with `degraded: true` and is not launchable;
- `enlace doctor`: a FAIL check per stubbed app, read from the live
  gateway’s `/_meta`, so a failure only the server’s own identity hits (a
  file only it cannot read) is still reported.

What is contained is narrow on purpose: an `asgi`-mode app’s import, or the
detection and loading of its sub-app. Platform configuration errors (a bad
`platform.toml`, an unknown `landing_app`, route conflicts, a plugin that
refuses its configuration) still refuse to boot, as do lifespan failures, an
app with `fail_hard = true` in its app.toml, and every app when the platform
sets `app_load_failure = "raise"`. The decision and its reasons are recorded
in `misc/docs/decisions/0001-loud-and-local-app-load-failures.md`.

### Module Attributes

| [`STUB_STATUS`](#enlace.degraded.STUB_STATUS)       | Status the stub answers every HTTP request with.                                                        |
|--------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------|
| [`STUB_HEADER`](#enlace.degraded.STUB_HEADER)       | Response header naming the condition, so a log line or a probe can tell a stub's 500 from an app's own. |
| [`DEGRADED_APPS_KEY`](#enlace.degraded.DEGRADED_APPS_KEY) | Platform `/_meta` key listing stubbed apps.                                                             |
| [`DEGRADED_KEY`](#enlace.degraded.DEGRADED_KEY)      | Per-app `/_meta` and `/_apps` key flagging a stubbed app.                                               |
| [`CONTAINED_MODES`](#enlace.degraded.CONTAINED_MODES)   | Modes whose load failures are contained.                                                                |

### Functions

| [`degraded_apps`](#enlace.degraded.degraded_apps)(config)                 | The apps that failed to load, by name, with their public summary.         |
|----------------------------------------------------------------------------------------|---------------------------------------------------------------------------|
| [`load_or_stub`](#enlace.degraded.load_or_stub)(strategy, app, platform) | Return `app`'s ASGI sub-app, or a stub when an asgi app cannot load.      |
| [`refuse_to_boot_if_required`](#enlace.degraded.refuse_to_boot_if_required)(platform)  | Raise the first recorded load failure the configuration says not to stub. |

### Classes

| [`UnavailableApp`](#enlace.degraded.UnavailableApp)(app_name, exception_type)   | Pure-ASGI stub standing in for an app that failed to load.   |
|---------------------------------------------------------------------------------------------|--------------------------------------------------------------|

### Exceptions

| [`EnlaceAppLoadError`](#enlace.degraded.EnlaceAppLoadError)   | An app could not be loaded and its configuration says not to stub it.   |
|-----------------------------------------------------------------------|-------------------------------------------------------------------------|

### enlace.degraded.CONTAINED_MODES *= frozenset({'asgi'})*

Modes whose load failures are contained. Only `asgi` imports app code
into the gateway; every other mode’s `make_asgi` failure is configuration.

### enlace.degraded.DEGRADED_APPS_KEY *= 'degraded_apps'*

Platform `/_meta` key listing stubbed apps. Present only when one is, so a
healthy `/_meta` stays quiet (the same convention as `manifest_error`).

### enlace.degraded.DEGRADED_KEY *= 'degraded'*

Per-app `/_meta` and `/_apps` key flagging a stubbed app.

### *exception* enlace.degraded.EnlaceAppLoadError

Bases: [`RuntimeError`](https://docs.python.org/3/builtins/exceptions.html#RuntimeError)

An app could not be loaded and its configuration says not to stub it.

Raised only when the original exception is not available to re-raise
(an `AppImportError` built by hand rather than from an exception).

### enlace.degraded.STUB_HEADER *= 'x-enlace-app-status'*

Response header naming the condition, so a log line or a probe can tell a
stub’s 500 from an app’s own.

### enlace.degraded.STUB_STATUS *= 500*

Status the stub answers every HTTP request with.

### *class* enlace.degraded.UnavailableApp(app_name, exception_type)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Pure-ASGI stub standing in for an app that failed to load.

Every HTTP request answers 500 with a short plain-text body naming the app
and the exception *class* — never the message or a traceback, which can
carry server paths and is logged instead. A websocket handshake is refused
(a close before accept: uvicorn answers it as an HTTP 403; the ASGI close
code is 1011). It has no `router`, so the gateway’s lifespan cascade
passes it by.

### enlace.degraded.degraded_apps(config)

The apps that failed to load, by name, with their public summary.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]]

```pycon
>>> degraded_apps(PlatformConfig())
{}
```

### enlace.degraded.load_or_stub(strategy, app, platform)

Return `app`’s ASGI sub-app, or a stub when an asgi app cannot load.

An app that discovery already recorded as un-importable is not imported a
second time. A contained compose-time failure is recorded on `app`
(mutated in place, like `updated_at`) so every reporting surface reads
the same record. A failure that must not be contained is re-raised with its
original traceback.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### enlace.degraded.refuse_to_boot_if_required(platform)

Raise the first recorded load failure the configuration says not to stub.

For callers that act on a discovered config *before* `build_backend` runs
— `enlace serve` spawns process-mode children and a supervised gateway —
so `fail_hard` and `app_load_failure = "raise"` still stop everything
up front, as they did when discovery raised.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)
