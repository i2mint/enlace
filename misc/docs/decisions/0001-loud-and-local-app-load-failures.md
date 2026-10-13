# ADR-0001: One app that cannot load gets a 500 stub, not the whole platform

- **Status:** Accepted
- **Date:** 2026-10-13
- **Issue:** [i2mint/enlace#37](https://github.com/i2mint/enlace/issues/37) (prerequisite: [#36](https://github.com/i2mint/enlace/issues/36))
- **Supersedes:** the rationale in the `_detect_app_type` docstring (`enlace/discover.py`), "this is intentionally NOT caught — silently swallowing import errors is an anti-pattern", as far as booting the gateway is concerned.

## Context

An `asgi`-mode app is mounted by importing its entry module into the gateway's own process. Until this decision, an exception raised by that import propagated out of `build_backend` (and, one step earlier, out of discovery). Under gunicorn, the worker failed to boot, the master shut down, and every app on the platform answered 502.

The failure policy was **loud and global**: never hide a broken app, and pay for that with the whole platform. The "loud" half is right. Silently swallowing an import error turns a broken app into a mystery. The "global" half is not a principle, though. It is a side effect of sharing one process, and [#1](https://github.com/i2mint/enlace/issues/1) already listed it as a problem ("Shared failure domain"). It is not a design goal.

It has happened in production. A deploy of an app with no Python in it fast-forwarded a shared dependency, and one unrelated app's import then failed with `ImportError: cannot import name …`. Every gated app on the platform went down until a human intervened, and the post-deploy smoke check never ran.

There is precedent for degrading instead of crashing: a corrupt deploy manifest degrades `/_meta` instead of crashing the server (`enlace/manifest.py`), and a flaky store must never break the `/_apps` listing (`enlace/compose.py`).

## Decision

The policy becomes **loud and local**. When an `asgi`-mode app cannot be imported or loaded, the gateway mounts a 500 stub at that app's route prefix and boots. Every other app serves normally.

**A stub is not a degraded success.** It must be as loud as the crash-loop it replaces, only not as wide. Every stub is reported, from one record (`AppConfig.import_error`), in four places:

| Surface | What it shows |
|---|---|
| The stub itself | `500`, `Cache-Control: no-store`, `X-Enlace-App-Status: load-failed`, and a body naming the app and the exception **class**. It never includes the message or the traceback, which can carry server paths. Websockets are refused with close code 1011. |
| Logs | One `ERROR` line per stubbed app at boot, with the full traceback. |
| `/_meta` | The platform payload gains `degraded_apps: {name: {exception_type, stage}}`. The app's own `/_meta` gains `degraded`. Both keys are absent when everything is healthy, so a healthy `/_meta` stays quiet. |
| `/_apps` | The app is listed with `degraded: true`, `launchable: false` and no `launch_url`. The built-in index marks it unavailable. |
| `enlace doctor` | A `FAIL` per stubbed app, read from the live gateway's `/_meta` (`degraded:<app>`). This catches a failure that only the server's own identity hits, such as a file the service user cannot read, which the doctor's own discovery would not reproduce. The existing static `import:<app>` FAIL and the per-app HTTP probe, which FAILs on 5xx, still apply. |

What is contained, exactly:

- **In scope:** the failure of an `asgi`-mode app's entry-module import during discovery (type detection), or of loading its sub-app in `build_backend` (for example, the configured `app` attribute is missing). The net is `Exception` plus `SystemExit`, because import runs arbitrary module code. The one production case of this kind was a `PermissionError`, and a module calling `sys.exit()` at import would otherwise end the worker. `KeyboardInterrupt` still escapes.
- **Out of scope; these still refuse to boot:** every platform configuration error. That covers an unparseable `platform.toml`, an unknown `landing_app`, route and name conflicts, an unknown mode, a broken `ENLACE_PLUGINS` entry, and a plugin that rejects its own configuration (such as an auth allowlist). It also covers `make_asgi` failures in any mode other than `asgi`: a proxy or plugin strategy failing is configuration, not app code. Lifespan failures stay out too (see question 3).

The library seam is unchanged: `discover_apps(..., on_import_error="raise")` remains the default for library callers. Only the gateway entry points, `create_app` (what gunicorn loads) and `enlace serve`, discover with `"record"` and let `build_backend` decide.

On a failed import, modules of the app's own package that loaded before the failure are dropped from `sys.modules` (`enlace.discover.forget_partial_imports`). Third-party modules are left alone, because another app may already hold them and a C extension cannot be loaded twice.

## The four open questions from the issue

1. **Every asgi app, or only a declared `shared` tier?** Every `asgi` app, by default. A tier would need the deploy platform's tier work to land first, and the stricter "rather absent than serving a 500" stance is already available per app (question 4) and per platform (`[platform] app_load_failure = "raise"`). If the tiering work lands, a tier can set these same knobs. No new mechanism is needed.
2. **Route conflicts?** They stay fatal. A conflict is a configuration error that names two known apps. Stubbing both would hide a real ambiguity about who owns a prefix, and stubbing one would pick a winner by accident. Unlike an import failure, a conflict is fully known before deploy: `enlace check` reports it.
3. **Lifespan failures?** They stay fatal in v1. They happen after import, inside the shared `AsyncExitStack` of `cascade_lifespan`, with earlier apps' lifespans already entered. Containing them means partial teardown, ordering guarantees and a different "stub after startup" path. That is a separate design, tracked in [i2mint/enlace#76](https://github.com/i2mint/enlace/issues/76).
4. **Per-app opt-out in v1?** Yes. `fail_hard = true` in an app's `app.toml` makes that app's load failure refuse to boot the platform, with the original exception and traceback. For the platform as a whole, `[platform] app_load_failure = "raise"` restores the previous behaviour without pinning an old enlace.

## Consequences

- One broken app now costs one app, not the platform. A deploy that breaks an unrelated app's import produces one 500ing app, an ERROR in the log, a red `/_meta` and a red `enlace doctor`, not a site-wide 502 and silence.
- The inverse risk is a platform that boots looking healthy while serving 500s. That risk is why every stub is reported in four places and why there is a test asserting that no stub is ever silent. Anything that alerts on platform health should read `degraded_apps` from `/_meta` or gate on `enlace doctor`'s exit code. A `200` from `/` no longer proves every app loaded.
- Containment covers availability, not isolation. A failed import can still have run side effects (environment variables, monkeypatches, global registries) that enlace cannot undo. An app that needs real isolation should run in `mode = "process"`.
- An app whose backend fails but whose frontend directory exists still has its static frontend served at `/{name}/`. Its API calls answer 500, and the launcher shows it as broken.
- Under the default policy, the gateway's discovery no longer raises for an un-importable app. Code that called `create_app()` expecting an exception must now check `degraded_apps`, or set `app_load_failure = "raise"`.
- `/_meta` names stubbed apps, including protected ones, with the exception class only. App names are already observable (a protected app's `/<name>/_meta` answers 401, an unknown one 404), and the deploy manifest in the same payload already names apps and connectors.
