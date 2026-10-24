# enlace

enlace – Compose, serve, and deploy multiple web apps from a single codebase.

Drop a Python module or a React app into a directory, and enlace discovers it,
mounts it, serves it, and optionally gates it behind auth – with zero boilerplate.

### Functions

| [`analytics_report`](#enlace.analytics_report)([app, days, config, store])     | Per-app report for the last `days` days: daily series + totals.              |
|---------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| [`build_backend`](#enlace.build_backend)(config, \*[, plugins, ...])        | Compose all app backends into a single ASGI application.                     |
| [`create_app`](#enlace.create_app)()                                     | App factory for Uvicorn's --factory flag.                                    |
| [`daily_counts`](#enlace.daily_counts)(store, app, \*[, days, today, ...]) | One merged record per day for the last `days` days, oldest first.            |
| [`diagnose_app`](#enlace.diagnose_app)(app_dir, \*[, app_name])            | Diagnose an app directory for enlace compatibility.                          |
| [`discover_apps`](#enlace.discover_apps)([config, on_import_error])         | High-level discovery: load config, discover apps, check conflicts.           |
| [`load_manifest`](#enlace.load_manifest)(app_name, manifest_dir, \*[, ...]) | Load the deploy manifest for one app, or return a minimal stub.              |
| [`load_platform_manifest`](#enlace.load_platform_manifest)(manifest_dir, \*[, ...])  | Load the platform-level deploy manifest (or a minimal stub).                 |
| [`page_shell`](#enlace.page_shell)(\*, api[, base, title, ...])          | The generic shell: `window.<global_name> = {api, base, title}`, then assets. |
| [`run_build`](#enlace.run_build)(app, \*[, extra_env, dry_run, check])  | Run an app's `install` (if any) then `build` commands.                       |
| [`serve`](#enlace.serve)(\*[, mode, apps_dir, apps_dirs, ...])      | Start the enlace backend server.                                             |
| [`skills_dir`](#enlace.skills_dir)()                                     | Return the path to this package's bundled skills directory.                  |
| [`validate_build`](#enlace.validate_build)(app)                              | Return human-readable problems with an app's `[build]` config.               |

### Classes

| [`AppAnalyticsConfig`](#enlace.AppAnalyticsConfig)(\*[, mode])                     | An app's `[analytics]` table in `app.toml`.                                 |
|-----------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------|
| [`AppConfig`](#enlace.AppConfig)(\*, name, route_prefix[, ...])           | Resolved configuration for a single discovered app.                         |
| [`AppImportError`](#enlace.AppImportError)(\*, exception_type, message[, ...]) | Why an app could not be loaded, recorded instead of raised.                 |
| [`BuildConfig`](#enlace.BuildConfig)(\*[, cwd, install, build, ...])        | Declarative build instructions for an app's compiled frontend.              |
| [`BuildResult`](#enlace.BuildResult)(app, cwd[, commands, ran, ...])        | Outcome of building one app.                                                |
| [`ConventionsConfig`](#enlace.ConventionsConfig)(\*[, entry_points, ...])         | Meta-conventions controlling how apps are discovered.                       |
| [`DeployHeadersMiddleware`](#enlace.DeployHeadersMiddleware)(app, \*, ...[, ...])       | Pure-ASGI middleware that adds X-Deploy-\* headers on every response.       |
| [`DeployManifest`](#enlace.DeployManifest)(\*[, schema_version, ...])          | What was deployed for one app (or the platform itself).                     |
| [`DeployMetaTagMiddleware`](#enlace.DeployMetaTagMiddleware)(app, \*, ...[, ...])       | Pure-ASGI middleware that injects deploy `<meta>` tags into HTML.           |
| [`DiagnosticReport`](#enlace.DiagnosticReport)(app_dir, app_name[, ...])         | Full diagnostic report for an app directory.                                |
| [`ExternalRef`](#enlace.ExternalRef)(\*[, sha, version])                    | Identity for an externally-installed dependency (e.g. an editable sibling). |
| [`Issue`](#enlace.Issue)(severity, category, summary[, ...])          | A single compatibility issue found during diagnosis.                        |
| [`JsonFileStore`](#enlace.JsonFileStore)(root)                                | `key -> dict`, one JSON file per key at `{root}/{key}.json`.                |
| [`PackagePageConfig`](#enlace.PackagePageConfig)(\*, package[, assets, ...])      | app.toml's `page_from_package`: the package that ships the app's page.      |
| [`PlatformAnalyticsConfig`](#enlace.PlatformAnalyticsConfig)(\*[, store_path, ...])     | The platform's `[analytics]` table in `platform.toml`.                      |
| [`PlatformConfig`](#enlace.PlatformConfig)(\*[, apps_dir, apps_dirs, ...])     | Resolved configuration for the entire platform.                             |
| [`ConventionDiscoverer`](#enlace.ConventionDiscoverer)([conventions, ...])           | Discovers apps by filesystem conventions.                                   |
| [`SourceRef`](#enlace.SourceRef)(\*[, sha, ref, dirty, local_path, ...])  | Git identity for a single source tree (app or platform).                    |

### Exceptions

| [`EnlaceConfigError`](#enlace.EnlaceConfigError)   | Raised when platform configuration is unusable at startup.   |
|----------------------------------------------------------------------|--------------------------------------------------------------|

### *class* enlace.AppAnalyticsConfig(, mode='none')

Bases: `BaseModel`

An app’s `[analytics]` table in `app.toml`. Absent means `none`.

Strict (unknown keys and modes are errors), so a typo fails at discovery
instead of silently collecting nothing — or something else.

#### *property* enabled *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

Whether this app’s page views are counted.

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {'extra': 'forbid'}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.AppConfig(\*, name, route_prefix, entry_module_path=None, app_type, app_attr='app', frontend_dir=None, source_dir=None, build=None, access='local', shared_password_env=None, allowed_users=<factory>, user_store=False, display_name='', provenance=<factory>, analytics=<factory>, rate_limit=<factory>, page_from_package=None, import_error=None, fail_hard=False, description='', keywords=<factory>, icon='', launchable=None, listed=True, updated_at=None, mode='asgi', command=None, port=None, socket=None, env=<factory>, health_check_path='/health', ready_timeout=30.0, restart_policy='on-failure', max_retries=5, restart_delay_ms=100, forward_platform_credentials=False, upstream_url=None, public_dir=None, \*\*extra_data)

Bases: `BaseModel`

Resolved configuration for a single discovered app.

`extra="allow"` lets plugin strategies (e.g. `enlace_docker`) carry
their own typed fields — `dockerfile`, `image`, `compose_file`,
`service`, etc. — without enlace having to enumerate them. The
plugin’s `BackendStrategy` reads them via attribute access
(`app.dockerfile`); enlace itself ignores them.

#### forward_platform_credentials *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

Forward the visitor’s platform session and CSRF credentials (cookies and
the `X-CSRF-Token` header) to the process. Off by default: a process app
is a separate program with its own dependencies, and bearer-token apps
such as MCP connectors never need them. Response filtering (no platform
`Set-Cookie`, `Clear-Site-Data` or `Service-Worker-Allowed`) applies
either way.

#### *property* has_frontend *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

Whether enlace serves a page for this app at `/{name}/`.

A frontend directory on disk, or a page shipped by a package
(`page_from_package`).

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {'extra': 'allow'}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.AppImportError(, exception_type, message, entry_module_path=None, stage='discover')

Bases: `BaseModel`

Why an app could not be loaded, recorded instead of raised.

Recorded at discovery (`on_import_error="record"` — see
[`enlace.discover.discover_apps()`](enlace.discover.html.md#enlace.discover.discover_apps)) or at compose time, when
[`enlace.compose.build_backend()`](enlace.compose.html.md#enlace.compose.build_backend) contains a per-app load failure and
mounts a 500 stub in the app’s place (see [`enlace.degraded`](enlace.degraded.html.md#module-enlace.degraded) and
`misc/docs/decisions/0001-loud-and-local-app-load-failures.md`).

Importing a module runs arbitrary module-level code, so `exception_type`
is not necessarily an `ImportError`: the failure seen in production was a
`PermissionError` from a dependency reading a root-only dotenv at import.

The exception object itself is kept privately ([`exception`](#enlace.AppImportError.exception)) so a
caller that must refuse to boot can re-raise it with its real traceback.
It is never serialized.

#### *property* exception *: [Optional](https://docs.python.org/3/library/typing.html#typing.Optional)[[BaseException](https://docs.python.org/3/builtins/exceptions.html#BaseException)]*

The original exception, when this record was built from one.

#### *classmethod* from_exception(exc, entry_module_path=None, , stage='discover')

Build a record from the exception the import (or load) raised.

* **Return type:**
  [`AppImportError`](enlace.base.html.md#enlace.base.AppImportError)

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

#### model_post_init(context,)

This function is meant to behave like a BaseModel method to initialize private attributes.

It takes context as an argument since that’s what pydantic-core passes when calling it.

* **Parameters:**
  * **self** (`BaseModel`) – The BaseModel instance.
  * **context** ([`Any`](https://docs.python.org/3/library/typing.html#typing.Any)) – The context.
* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### public_summary()

What may be shown to anyone: the exception class and the stage.

Never the message — it can carry server paths or configuration values
(the production `PermissionError` named the dotenv’s path).

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### *class* enlace.BuildConfig(\*, cwd=None, install=None, build=None, env_vars=<factory>, outputs=<factory>)

Bases: `BaseModel`

Declarative build instructions for an app’s compiled frontend.

Mirrors the `[build]` table in `app.toml`. It describes the app, not
its deployment — a Vite/Next/esbuild app has a `build` command whether
or not enlace serves it — so it keeps the “apps don’t know about enlace”
principle intact. enlace (or any deployer) runs these as an explicit step
via `enlace build`; enlace never builds at request time.

`install` / `build` accept either a single string (split with
`shlex`) or a list of argv tokens. `cwd` is resolved relative to the
app directory at discovery time; `None` means “the app directory”.
`env_vars` is a *hint* of which env vars the build honours (e.g.
`VITE_API_BASE`) so deployers know what they may inject — enlace does
not choose values. `outputs` is an optional hint of produced paths.

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.BuildResult(app, cwd, commands=list(), ran=False, returncode=0)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Outcome of building one app.

### *class* enlace.ConventionDiscoverer(conventions=None, , on_import_error='raise')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Discovers apps by filesystem conventions.

Walks the apps directory, finds entry points, detects app types,
loads TOML overrides, and returns validated AppConfig objects.

* **Parameters:**
  * **conventions** ([`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`ConventionsConfig`](enlace.base.html.md#enlace.base.ConventionsConfig)]) – Meta-conventions controlling discovery behavior.
  * **on_import_error** ([`Literal`](https://docs.python.org/3/library/typing.html#typing.Literal)[`'raise'`, `'record'`]) – What to do when an app’s entry module fails to
    import. `"raise"` (default) propagates, as it always has;
    `"record"` stores an `AppImportError` on the app’s config
    and carries on with the remaining apps.

#### discover(apps_dir)

Discover all apps in the given directory.

* **Parameters:**
  **apps_dir** ([`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)) – Path to the directory containing app subdirectories.
* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`AppConfig`](enlace.base.html.md#enlace.base.AppConfig)]
* **Returns:**
  List of AppConfig objects, sorted by name.

#### discover_app_dir(app_dir)

Discover a single directory that IS the app itself.

Unlike discover(), which walks children of a container directory,
this treats app_dir itself as the app directory.

* **Parameters:**
  **app_dir** ([`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)) – Path to the app directory (the directory IS the app).
* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`AppConfig`](enlace.base.html.md#enlace.base.AppConfig)]
* **Returns:**
  AppConfig if the directory is a valid app, None otherwise.

### *class* enlace.ConventionsConfig(, entry_points=['server.py', 'app.py', 'main.py'], app_attr='app', frontend_dir='frontend')

Bases: `BaseModel`

Meta-conventions controlling how apps are discovered.

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.DeployHeadersMiddleware(app, , manifests_by_prefix, platform_manifest=None)

Bases: `_PrefixManifestMiddleware`

Pure-ASGI middleware that adds X-Deploy-\* headers on every response.

The header values come from the manifest matching the longest registered
prefix (route_prefix or frontend mount path) for the request path. If no
per-app prefix matches, the platform manifest fills in. Headers are only
added when the underlying value is present — a stub manifest produces a
short header set (just `X-Deploy-App`), not bogus SHAs. Values are
percent-encoded where they are not plain ASCII (see `header_value()`).

### *class* enlace.DeployManifest(\*, schema_version=1, app, deployed_at=None, deployer=None, platform=None, app_source=<factory>, platform_source=<factory>, externals=<factory>, enlace_version=None, extra=<factory>)

Bases: `BaseModel`

What was deployed for one app (or the platform itself).

`app_source` and `platform_source` use the same shape so consumers can
treat them uniformly. `externals` is a free-form map keyed by package
name — the manifest format is intentionally permissive about which keys
appear, since which externals matter varies by app.

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.DeployMetaTagMiddleware(app, , manifests_by_prefix, platform_manifest=None)

Bases: `_PrefixManifestMiddleware`

Pure-ASGI middleware that injects deploy `<meta>` tags into HTML.

For `text/html` responses on app-mounted paths, inserts
`<meta name="x-deploy-sha" ...>` and `<meta name="x-deploy-time" ...>`
into `<head>`. This is what unlocks the browser-cache diagnostic: a
page can compare its embedded SHA against `/_meta` to tell “deploy
didn’t take” from “browser is serving a stale cache”.

Only HTML is touched — JSON, JS, and other assets stream through
untouched (and carry the `X-Deploy-*` headers instead). Because it
rewrites the body it must run **inside** any compression middleware
(e.g. GZip), so it sees and edits uncompressed bytes.

### *class* enlace.DiagnosticReport(app_dir, app_name, issues=list(), has_backend=False, has_frontend=False, backend_framework='', frontend_framework='', entry_point=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Full diagnostic report for an app directory.

#### format_text()

Human-readable formatted report.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

#### *property* is_enlaceable *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

True if no CRITICAL issues were found.

### *exception* enlace.EnlaceConfigError

Bases: [`RuntimeError`](https://docs.python.org/3/builtins/exceptions.html#RuntimeError)

Raised when platform configuration is unusable at startup.

Distinct from `ValueError` / `RuntimeError` so callers and tests can
target this specific class.

### *class* enlace.ExternalRef(, sha=None, version=None)

Bases: `BaseModel`

Identity for an externally-installed dependency (e.g. an editable sibling).

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.Issue(severity, category, summary, file_path=None, line_number=None, detail='', suggestion='', breaks_standalone=False)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A single compatibility issue found during diagnosis.

`category` is a `Category` for built-in checks, but plugin diagnosers
(registered via the `enlace.diagnosers` entry-point group) may pass a
plain string category — the report renders both.

### *class* enlace.JsonFileStore(root)

Bases: [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)

`key -> dict`, one JSON file per key at `{root}/{key}.json`.

Keys are `/`-separated relative paths of URL-safe segments. Writes are
atomic (temp file + `os.replace`), so a reader never sees half a record.
Stdlib only; swap in any `MutableMapping` (e.g. a `dol` store over S3)
through the `store` argument of `make_analytics()` or
`build_backend`. Two optional extras the maintenance uses when present:
[`exclusive()`](#enlace.JsonFileStore.exclusive) (a cross-process lock) and [`sweep_temp_files()`](#enlace.JsonFileStore.sweep_temp_files).

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

### *class* enlace.PackagePageConfig(\*, package, assets='data/ui', assets_route='ui', files=<factory>, shell=None, global_name='', title='', scripts=<factory>, stylesheets=<factory>)

Bases: `BaseModel`

app.toml’s `page_from_package`: the package that ships the app’s page.

A bare string is the package name, every other field at its default.

```pycon
>>> PackagePageConfig.model_validate("songleaf").assets
'data/ui'
>>> PackagePageConfig(package="my_pkg.sub").global_name
'MY_PKG'
```

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {'extra': 'forbid'}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.PlatformAnalyticsConfig(, store_path=None, retention_days=395, timezone='UTC', flush_interval_seconds=10.0, max_values_per_dimension=500, honor_opt_out_signals=True, exclude_prefixes=('/_', '/auth/'), opt_out_cookie='enlace_analytics_opt_out')

Bases: `BaseModel`

The platform’s `[analytics]` table in `platform.toml`.

Every field has a working default; the table is only needed to change one.
Validated at config load, so `enlace check` catches a bad value before a
boot does.

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {'extra': 'forbid'}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.PlatformConfig(\*, apps_dir=None, apps_dirs=<factory>, app_dirs=<factory>, shared_assets_dir=None, manifest_dir=None, index_page=True, landing_app=None, domain='localhost', cors_origins=<factory>, backend_port=8000, app_load_failure='stub', frontend_port=3000, process_port_start=9100, socket_dir=PosixPath('/tmp/enlace'), conventions=<factory>, apps=<factory>, auth=<factory>, stores=<factory>, app_meta=<factory>, analytics=<factory>, trusted_proxies=<factory>, trust_unix_socket=False, foreign_tables=<factory>)

Bases: `BaseModel`

Resolved configuration for the entire platform.

#### *property* all_source_dirs *: [list](https://docs.python.org/3/builtins/stdtypes.html#list)[[Path](https://docs.python.org/3/library/pathlib.html#pathlib.Path)]*

All directories to watch (for reload, etc.).

#### check_conflicts()

Check for name and route conflicts across all apps.

Returns all conflicts found (not just the first), so the user can fix
them all at once.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

#### *classmethod* from_toml(path=PosixPath('platform.toml'))

Load configuration from a TOML file, falling back to defaults.

Relative `apps_dirs` / `app_dirs` / `shared_assets_dir` entries
in the TOML are resolved against the **TOML file’s own directory**,
not the process working directory. This makes a platform config
host-portable: the same file works wherever the repo is checked out,
as long as sibling app repos keep their relative layout. Absolute
(and `~`-prefixed) paths are left untouched.

Reads environment variables as overrides (applied *after* relative
resolution, so env values are taken verbatim — host-specific by
design):

- ENLACE_APPS_DIRS (pathsep-delimited): container directories
- ENLACE_APP_DIRS (pathsep-delimited): individual app directories
- ENLACE_APPS_DIR (legacy): single container directory

Top-level tables enlace does not read are logged as warnings (and
kept on [`unknown_tables`](#enlace.PlatformConfig.unknown_tables)) unless `[platform] foreign_tables`
lists them; they are never an error, since a platform.toml may carry
tables for other tools.

* **Parameters:**
  **path** ([`Union`](https://docs.python.org/3/library/typing.html#typing.Union)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)]) – Path to platform.toml (a `str` or `Path`). If the file
  doesn’t exist, returns a PlatformConfig with all default values.
* **Return type:**
  [`PlatformConfig`](enlace.base.html.md#enlace.base.PlatformConfig)

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

#### model_post_init(context,)

This function is meant to behave like a BaseModel method to initialize private attributes.

It takes context as an argument since that’s what pydantic-core passes when calling it.

* **Parameters:**
  * **self** (`BaseModel`) – The BaseModel instance.
  * **context** ([`Any`](https://docs.python.org/3/library/typing.html#typing.Any)) – The context.
* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### *property* unknown_tables *: [list](https://docs.python.org/3/builtins/stdtypes.html#list)[[str](https://docs.python.org/3/builtins/stdtypes.html#str)]*

Unclaimed top-level tables in the loaded platform.toml, as messages.

### *class* enlace.SourceRef(, sha=None, ref=None, dirty=None, local_path=None, committed_at=None)

Bases: `BaseModel`

Git identity for a single source tree (app or platform).

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### enlace.analytics_report(app='', , days=30, config=None, store=None)

Per-app report for the last `days` days: daily series + totals.

With no `app`, reports every app that has data. `config` defaults to
`platform.toml` in the current directory (only its `[analytics]` table
is used; no app is imported).

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### enlace.build_backend(config, , plugins=(), analytics_store=None)

Compose all app backends into a single ASGI application.

For each discovered app:

- mode=asgi, asgi_app: mount the ASGI object at the route prefix
- mode=asgi, functions: build an APIRouter with POST routes and include it
- mode=process/external: mount a reverse proxy at the route prefix
- mode=static: mount StaticFiles at the route prefix
- frontend_only (mode=asgi): skip (no backend to mount)

An asgi app that cannot be imported or loaded does not take the platform
down: a 500 stub is mounted in its place and the failure is reported in
the logs, `/_meta`, `/_apps` and `enlace doctor` (loud and local —
see [`enlace.degraded`](enlace.degraded.html.md#module-enlace.degraded)). `config.app_load_failure = "raise"`, or
`fail_hard = true` in an app’s app.toml, refuses to boot instead.
Platform configuration errors always refuse to boot.

* **Parameters:**
  * **config** ([`PlatformConfig`](enlace.base.html.md#enlace.base.PlatformConfig)) – Platform configuration with apps already discovered.
  * **plugins** ([`Sequence`](https://docs.python.org/3/library/typing.html#typing.Sequence)[[`Callable`](https://docs.python.org/3/library/typing.html#typing.Callable)[[`FastAPI`, [`PlatformConfig`](enlace.base.html.md#enlace.base.PlatformConfig)], [`None`](https://docs.python.org/3/builtins/constants.html#None)]]) – Compose-time plugins, e.g. `enlace_auth.plugin`.
  * **analytics_store** ([`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`MutableMapping`](https://docs.python.org/3/library/typing.html#typing.MutableMapping)]) – Where page-view analytics go, for apps that opted in
    (any `MutableMapping[str, dict]`, e.g. a `dol` store). Default:
    JSON files under `[analytics].store_path`. See enlace.analytics.
* **Return type:**
  `FastAPI`
* **Returns:**
  A FastAPI application with all sub-apps mounted.

### enlace.create_app()

App factory for Uvicorn’s –factory flag.

Loads platform config, discovers apps, checks conflicts,
and builds the composed backend.

Discovery *records* an app whose entry module fails to import rather than
raising, so `build_backend` can mount a 500 stub for that app alone
(see [`enlace.degraded`](enlace.degraded.html.md#module-enlace.degraded)). Platform configuration errors, including a
broken `ENLACE_PLUGINS` entry, still raise here.

Plugins are loaded from the `ENLACE_PLUGINS` env var: a comma-separated
list of `module:attribute` pairs, e.g.:

```default
ENLACE_PLUGINS=enlace_auth:plugin
```

Each resolved object must be a callable
`(parent: FastAPI, config: PlatformConfig) -> None`.

* **Return type:**
  `FastAPI`

### enlace.daily_counts(store, app, , days=30, today=None, timezone='UTC', index=None)

One merged record per day for the last `days` days, oldest first.

Days with no data are included, with zero counts, so the series has no
gaps. Each item is `{"date": ..., "pageviews": ..., "bot_hits": ...,
"paths": {...}, "referrers": {...}, "languages": {...}, "devices": {...}}`.
Pass `index` (from one scan) when reading several apps.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)]

### enlace.diagnose_app(app_dir, , app_name='')

Diagnose an app directory for enlace compatibility.

Scans for hardcoded URLs, CORS middleware, SSR requirements, missing
entry points, and other patterns that prevent or complicate mounting
under the enlace platform. Plugin diagnosers registered via the
`enlace.diagnosers` entry-point group run after the built-in checks.

* **Parameters:**
  * **app_dir** – Path to the app directory to diagnose.
  * **app_name** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – Override app name (defaults to directory name).
* **Return type:**
  [`DiagnosticReport`](enlace.diagnose.html.md#enlace.diagnose.DiagnosticReport)
* **Returns:**
  DiagnosticReport with all findings.

### enlace.discover_apps(config=None, , on_import_error='raise')

High-level discovery: load config, discover apps, check conflicts.

Iterates over all configured source directories:

- `config.apps_dirs`: container directories (walk children)
- `config.app_dirs`: individual app directories (discover directly)

* **Parameters:**
  * **config** ([`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`PlatformConfig`](enlace.base.html.md#enlace.base.PlatformConfig)]) – Platform configuration. If None, loads from platform.toml.
  * **on_import_error** ([`Literal`](https://docs.python.org/3/library/typing.html#typing.Literal)[`'raise'`, `'record'`]) – What to do when an `asgi`-mode app’s entry module
    fails to import. `"raise"` (default) propagates, exactly as
    before this argument existed — what `serve` and `build_backend`
    want, because a gateway must not boot pretending an app is fine.
    `"record"` records the failure on that app’s `AppConfig`
    (`import_error`) and keeps discovering, so a diagnostic caller
    can *report* the broken app alongside the healthy ones instead of
    dying on it.
* **Return type:**
  [`PlatformConfig`](enlace.base.html.md#enlace.base.PlatformConfig)
* **Returns:**
  PlatformConfig with apps populated.
* **Raises:**
  [**RuntimeError**](https://docs.python.org/3/builtins/exceptions.html#RuntimeError) – If name or route conflicts are detected.

### enlace.load_manifest(app_name, manifest_dir, , enlace_version=None)

Load the deploy manifest for one app, or return a minimal stub.

The stub has just `app` (the name we were asked about) and
`enlace_version` filled in. That keeps the diagnostic plumbing working
even before any deploy tool starts writing manifests.

A manifest file that exists but can’t be used – unreadable, corrupt JSON,
not a JSON object, or valid JSON the schema rejects (e.g. an unknown
`deployer`) – also yields the stub, so it can neither stop the backend
from starting nor turn `/_meta` into a 500. It is logged, and the stub
carries `extra[MANIFEST_ERROR_KEY]` saying why.

* **Return type:**
  [`DeployManifest`](enlace.manifest.html.md#enlace.manifest.DeployManifest)

### enlace.load_platform_manifest(manifest_dir, , enlace_version=None)

Load the platform-level deploy manifest (or a minimal stub).

* **Return type:**
  [`DeployManifest`](enlace.manifest.html.md#enlace.manifest.DeployManifest)

### enlace.page_shell(, api, base='', title='', global_name='APP', scripts=('app.js',), stylesheets=('style.css',), assets_route='ui')

The generic shell: `window.<global_name> = {api, base, title}`, then assets.

`api` is where the app’s API is mounted (its files are under
`{api}/{assets_route}/`), `base` where the page lives.

```pycon
>>> shell = page_shell(api="/api/demo", base="/demo", title="Demo")
>>> '<script src="/api/demo/ui/app.js"></script>' in shell
True
>>> 'window.APP = {"api": "/api/demo", "base": "/demo", "title": "Demo"}' in shell
True
```

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### enlace.run_build(app, , extra_env=None, dry_run=False, check=True)

Run an app’s `install` (if any) then `build` commands.

* **Parameters:**
  * **app** ([`AppConfig`](enlace.base.html.md#enlace.base.AppConfig)) – The app whose `[build]` section to run.
  * **extra_env** ([`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]]) – Values to overlay on the inherited environment (e.g. the
    deployer’s `VITE_API_BASE=/api/{name}`). enlace itself does not
    choose values; callers do.
  * **dry_run** ([`bool`](https://docs.python.org/3/builtins/functions.html#bool)) – Resolve and record the commands without executing them.
  * **check** ([`bool`](https://docs.python.org/3/builtins/functions.html#bool)) – Raise `subprocess.CalledProcessError` on nonzero exit.
* **Return type:**
  [`BuildResult`](enlace.build.html.md#enlace.build.BuildResult)
* **Returns:**
  A [`BuildResult`](#enlace.BuildResult) describing what was (or would be) run.

### enlace.serve(, mode='dev', apps_dir='', apps_dirs='', app_dirs='', port=0, host='', config='platform.toml')

Start the enlace backend server.

Reads platform.toml as the source of truth for directories and ports.
CLI arguments override TOML values when provided.

* **Parameters:**
  * **mode** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – ‘dev’ for development (hot reload) or ‘prod’ for production.
  * **apps_dir** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – Path to the apps directory (backward compat, overrides TOML).
  * **apps_dirs** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – Comma-separated container directories (overrides TOML).
  * **app_dirs** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – Comma-separated individual app directories (overrides TOML).
  * **port** ([`int`](https://docs.python.org/3/builtins/functions.html#int)) – Port to listen on (0 = use TOML value or default 8000).
  * **host** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – Host to bind to (empty = use default 127.0.0.1).
  * **config** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – Path to platform.toml.

### enlace.skills_dir()

Return the path to this package’s bundled skills directory.

* **Return type:**
  [`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)

### enlace.validate_build(app)

Return human-readable problems with an app’s `[build]` config.

Pure validation — never runs commands. Checks that the section, if
present, has a `build` command and that its working directory exists.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### Modules

| [`access`](enlace.access.html.md#module-enlace.access)                 | Who may reach an app: the one predicate the gate and the launcher both call.         |
|----------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------|
| [`analytics`](enlace.analytics.html.md#module-enlace.analytics)           | Privacy-first page-view analytics, turned on per app.                                |
| [`app_icons`](enlace.app_icons.html.md#module-enlace.app_icons)           | One icon per app, served in every form a browser or phone asks for.                  |
| [`app_identity`](enlace.app_identity.html.md#module-enlace.app_identity)     | The app identity checklist: what every public-facing app's page must carry.          |
| [`appmeta`](enlace.appmeta.html.md#module-enlace.appmeta)               | App metadata: harvest, resolve, and render titles / descriptions / keywords / icons. |
| [`base`](enlace.base.html.md#module-enlace.base)                     | Core data structures for enlace platform configuration.                              |
| [`build`](enlace.build.html.md#module-enlace.build)                   | Run and validate declarative app builds from `app.toml` `[build]`.                   |
| [`compose`](enlace.compose.html.md#module-enlace.compose)               | ASGI app composition for enlace.                                                     |
| [`degraded`](enlace.degraded.html.md#module-enlace.degraded)             | Loud-and-local app-load failures: a 500 stub for the one app that broke.             |
| [`diagnose`](enlace.diagnose.html.md#module-enlace.diagnose)             | Diagnose app compatibility with enlace.                                              |
| [`discover`](enlace.discover.html.md#module-enlace.discover)             | Convention-based app discovery for enlace.                                           |
| [`doctor`](enlace.doctor.html.md#module-enlace.doctor)                 | Post-deploy smoke checks for a running enlace gateway.                               |
| [`frontend`](enlace.frontend.html.md#module-enlace.frontend)             | SPA-aware static file serving for enlace.                                            |
| [`gzip_selective`](enlace.gzip_selective.html.md#module-enlace.gzip_selective) | Compression that knows what it must not compress.                                    |
| [`html_rewrite`](enlace.html_rewrite.html.md#module-enlace.html_rewrite)     | Rewrite HTML response bodies from pure-ASGI middleware.                              |
| [`manifest`](enlace.manifest.html.md#module-enlace.manifest)             | Deploy manifest: build-identity for diagnosing "what is actually deployed".          |
| [`package_page`](enlace.package_page.html.md#module-enlace.package_page)     | Serve an app's page from its Python package: the shell and its `/ui/` assets.        |
| [`proxy`](enlace.proxy.html.md#module-enlace.proxy)                   | Lightweight ASGI reverse proxy for process and external backends.                    |
| [`ratelimit`](enlace.ratelimit.html.md#module-enlace.ratelimit)           | Opt-in, per-app, per-client rate limits for mounted apps.                            |
| [`strategies`](enlace.strategies.html.md#module-enlace.strategies)         | Backend-strategy registry and built-in strategies for enlace.                        |
| [`supervise`](enlace.supervise.html.md#module-enlace.supervise)           | Dev-mode process supervisor for enlace.                                              |
| [`util`](enlace.util.html.md#module-enlace.util)                     | Internal helpers for enlace.                                                         |
