# enlace.doctor

Post-deploy smoke checks for a running enlace gateway.

Complements `enlace check` (static config validation) by probing a live
gateway over HTTP. Catches silent-degradation failures that static analysis
can’t — the incident that motivated this (i2mint/enlace#11) was a gateway
booting cleanly with auth un-mounted because the signing key was missing at
startup; auth-specific checks for that scenario live in
`enlace_auth.diagnostics`.

Design:

- Pure stdlib `urllib` for HTTP. No new deps; this must work in minimal
  deploy venvs.
- Every check is a `Check(name, status, detail)`. The runner collects all
  of them and returns a `Report` so callers can emit pretty text OR JSON.
- `detail` is a short human-readable string. Structured payloads go in
  `extra` (dict) so `--json` consumers don’t re-parse prose.
- An app enlace could not even import is reported as a `FAIL` check, not as
  a traceback — see `_check_app_imports` and `discover_apps`’
  `on_import_error`. A health tool that dies on the failure it exists to
  detect reports nothing about the thirty apps that are fine.
- Plugins can supply extra static or HTTP probes via `extra_static_checks`
  and `extra_http_checks` on `run_doctor`. Those are also discovered
  automatically from the plugins the platform is configured to load — see
  `discover_plugin_checks()`. A check nobody runs is not a check, and the
  > hand-wiring alternative meant a plugin could ship a correct diagnosis of its
  > own failure mode that never once executed.

### Functions

| [`run_doctor`](#enlace.doctor.run_doctor)(config, \*[, base_url, timeout, ...])   | Run all checks and return a `Report`.   |
|-----------------------------------------------------------------------------------------------------|-----------------------------------------|

### Classes

| [`Check`](#enlace.doctor.Check)(name, status[, detail, extra])   | Result of a single probe.                   |
|-----------------------------------------------------------------------------------------|---------------------------------------------|
| [`Report`](#enlace.doctor.Report)(base_url[, checks])             | Aggregate report for one run of the doctor. |

### *class* enlace.doctor.Check(name, status, detail='', extra=<factory>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Result of a single probe.

### *class* enlace.doctor.Report(base_url, checks=<factory>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Aggregate report for one run of the doctor.

#### format_text()

Pretty-print as a human-readable report.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

#### *property* ok *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

True iff no check failed. Warnings don’t flip the result.

### enlace.doctor.run_doctor(config, , base_url=None, timeout=5.0, app_filter=None, include_env_checks=True, extra_static_checks=(), extra_http_checks=())

Run all checks and return a `Report`.

* **Parameters:**
  * **config** ([`PlatformConfig`](enlace.base.html.md#enlace.base.PlatformConfig)) – Platform config to check against.
  * **base_url** ([`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]) – When set, HTTP probes are run against this URL. When
    unset, only static checks run.
  * **timeout** ([`float`](https://docs.python.org/3/builtins/functions.html#float)) – Per-request timeout for HTTP probes.
  * **app_filter** ([`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Iterable`](https://docs.python.org/3/library/typing.html#typing.Iterable)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]]) – If given, only probe these app names (static checks
    still run across all apps).
  * **include_env_checks** ([`bool`](https://docs.python.org/3/builtins/functions.html#bool)) – Forwarded to `extra_static_checks` callbacks
    via `config` (those that read env vars should respect their
    caller’s intent — see `enlace_auth.diagnostics` for the
    convention). enlace itself has no env-var checks of its own.
  * **extra_static_checks** ([`Iterable`](https://docs.python.org/3/library/typing.html#typing.Iterable)[[`Callable`](https://docs.python.org/3/library/typing.html#typing.Callable)[[[`PlatformConfig`](enlace.base.html.md#enlace.base.PlatformConfig)], [`Iterable`](https://docs.python.org/3/library/typing.html#typing.Iterable)[[`Check`](#enlace.doctor.Check)]]]) – Plugin-provided static checks. Each is a
    callable that receives `config` and returns `Iterable[Check]`.
  * **extra_http_checks** ([`Iterable`](https://docs.python.org/3/library/typing.html#typing.Iterable)[[`Callable`](https://docs.python.org/3/library/typing.html#typing.Callable)[[[`PlatformConfig`](enlace.base.html.md#enlace.base.PlatformConfig), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`float`](https://docs.python.org/3/builtins/functions.html#float)], [`Iterable`](https://docs.python.org/3/library/typing.html#typing.Iterable)[[`Check`](#enlace.doctor.Check)]]]) – Plugin-provided HTTP checks. Each is a callable
    that receives `(config, base_url, timeout)` and returns
    `Iterable[Check]`. Only invoked when `base_url` is set.
* **Return type:**
  [`Report`](#enlace.doctor.Report)
