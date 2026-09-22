# enlace

enlace – Compose, serve, and deploy multiple web apps from a single codebase.

Drop a Python module or a React app into a directory, and enlace discovers it,
mounts it, serves it, and optionally gates it behind auth – with zero boilerplate.

### Functions

| [`build_backend`](#enlace.build_backend)(config, \*[, plugins])             | Compose all app backends into a single ASGI application.           |
|---------------------------------------------------------------------------------------------------|--------------------------------------------------------------------|
| [`create_app`](#enlace.create_app)()                                     | App factory for Uvicorn's --factory flag.                          |
| [`diagnose_app`](#enlace.diagnose_app)(app_dir, \*[, app_name])            | Diagnose an app directory for enlace compatibility.                |
| [`discover_apps`](#enlace.discover_apps)([config, on_import_error])         | High-level discovery: load config, discover apps, check conflicts. |
| [`load_manifest`](#enlace.load_manifest)(app_name, manifest_dir, \*[, ...]) | Load the deploy manifest for one app, or return a minimal stub.    |
| [`load_platform_manifest`](#enlace.load_platform_manifest)(manifest_dir, \*[, ...])  | Load the platform-level deploy manifest (or a minimal stub).       |
| [`run_build`](#enlace.run_build)(app, \*[, extra_env, dry_run, check])  | Run an app's `install` (if any) then `build` commands.             |
| [`serve`](#enlace.serve)(\*[, mode, apps_dir, apps_dirs, ...])      | Start the enlace backend server.                                   |
| [`skills_dir`](#enlace.skills_dir)()                                     | Return the path to this package's bundled skills directory.        |
| [`validate_build`](#enlace.validate_build)(app)                              | Return human-readable problems with an app's `[build]` config.     |

### Classes

| [`AppConfig`](#enlace.AppConfig)(\*\*data)                          | Resolved configuration for a single discovered app.                         |
|-----------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------|
| [`AppImportError`](#enlace.AppImportError)(\*\*data)                     | Why an app's entry module could not be imported at discovery time.          |
| [`BuildConfig`](#enlace.BuildConfig)(\*\*data)                        | Declarative build instructions for an app's compiled frontend.              |
| [`BuildResult`](#enlace.BuildResult)(app, cwd[, commands, ran, ...])  | Outcome of building one app.                                                |
| [`ConventionsConfig`](#enlace.ConventionsConfig)(\*\*data)                  | Meta-conventions controlling how apps are discovered.                       |
| [`DeployHeadersMiddleware`](#enlace.DeployHeadersMiddleware)(app, \*, ...[, ...]) | Pure-ASGI middleware that adds X-Deploy-\* headers on every response.       |
| [`DeployManifest`](#enlace.DeployManifest)(\*\*data)                     | What was deployed for one app (or the platform itself).                     |
| [`DeployMetaTagMiddleware`](#enlace.DeployMetaTagMiddleware)(app, \*, ...[, ...]) | Pure-ASGI middleware that injects deploy `<meta>` tags into HTML.           |
| [`DiagnosticReport`](#enlace.DiagnosticReport)(app_dir, app_name[, ...])   | Full diagnostic report for an app directory.                                |
| [`ExternalRef`](#enlace.ExternalRef)(\*\*data)                        | Identity for an externally-installed dependency (e.g. an editable sibling). |
| [`Issue`](#enlace.Issue)(severity, category, summary[, ...])    | A single compatibility issue found during diagnosis.                        |
| [`PlatformConfig`](#enlace.PlatformConfig)(\*\*data)                     | Resolved configuration for the entire platform.                             |
| [`ConventionDiscoverer`](#enlace.ConventionDiscoverer)([conventions, ...])     | Discovers apps by filesystem conventions.                                   |
| [`SourceRef`](#enlace.SourceRef)(\*\*data)                          | Git identity for a single source tree (app or platform).                    |

### Exceptions

| [`EnlaceConfigError`](#enlace.EnlaceConfigError)   | Raised when platform configuration is unusable at startup.   |
|----------------------------------------------------------------------|--------------------------------------------------------------|

### *class* enlace.AppConfig(\*\*data)

Bases: `BaseModel`

Resolved configuration for a single discovered app.

`extra="allow"` lets plugin strategies (e.g. `enlace_docker`) carry
their own typed fields — `dockerfile`, `image`, `compose_file`,
`service`, etc. — without enlace having to enumerate them. The
plugin’s `BackendStrategy` reads them via attribute access
(`app.dockerfile`); enlace itself ignores them.

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {'extra': 'allow'}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.AppImportError(\*\*data)

Bases: `BaseModel`

Why an app’s entry module could not be imported at discovery time.

Recorded (instead of raised) when discovery runs with
`on_import_error="record"` — see [`enlace.discover.discover_apps()`](enlace.discover.html.md#enlace.discover.discover_apps).
Importing a module runs arbitrary module-level code, so `exception_type`
is not necessarily an `ImportError`: the failure seen in production was a
`PermissionError` from a dependency reading a root-only dotenv at import.

#### *classmethod* from_exception(exc, entry_module_path=None)

Build a record from the exception the import raised.

* **Return type:**
  [`AppImportError`](enlace.base.html.md#enlace.base.AppImportError)

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.BuildConfig(\*\*data)

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

### *class* enlace.BuildResult(app, cwd, commands=<factory>, ran=False, returncode=0)

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

### *class* enlace.ConventionsConfig(\*\*data)

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
short header set (just `X-Deploy-App`), not bogus SHAs.

### *class* enlace.DeployManifest(\*\*data)

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

### *class* enlace.DiagnosticReport(app_dir, app_name, issues=<factory>, has_backend=False, has_frontend=False, backend_framework='', frontend_framework='', entry_point=None)

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

### *class* enlace.ExternalRef(\*\*data)

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

### *class* enlace.PlatformConfig(\*\*data)

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

* **Parameters:**
  **path** ([`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)) – Path to platform.toml. If the file doesn’t exist, returns
  a PlatformConfig with all default values.
* **Return type:**
  [`PlatformConfig`](enlace.base.html.md#enlace.base.PlatformConfig)

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.SourceRef(\*\*data)

Bases: `BaseModel`

Git identity for a single source tree (app or platform).

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### enlace.build_backend(config, , plugins=())

Compose all app backends into a single ASGI application.

For each discovered app:

- mode=asgi, asgi_app: mount the ASGI object at the route prefix
- mode=asgi, functions: build an APIRouter with POST routes and include it
- mode=process/external: mount a reverse proxy at the route prefix
- mode=static: mount StaticFiles at the route prefix
- frontend_only (mode=asgi): skip (no backend to mount)

* **Parameters:**
  **config** ([`PlatformConfig`](enlace.base.html.md#enlace.base.PlatformConfig)) – Platform configuration with apps already discovered.
* **Return type:**
  `FastAPI`
* **Returns:**
  A FastAPI application with all sub-apps mounted.

### enlace.create_app()

App factory for Uvicorn’s –factory flag.

Loads platform config, discovers apps, checks conflicts,
and builds the composed backend.

Plugins are loaded from the `ENLACE_PLUGINS` env var: a comma-separated
list of `module:attribute` pairs, e.g.:

```default
ENLACE_PLUGINS=enlace_auth:plugin
```

Each resolved object must be a callable
`(parent: FastAPI, config: PlatformConfig) -> None`.

* **Return type:**
  `FastAPI`

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

| [`appmeta`](enlace.appmeta.html.md#module-enlace.appmeta)               | App metadata: harvest, resolve, and render titles / descriptions / keywords / icons.   |
|----------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------|
| [`base`](enlace.base.html.md#module-enlace.base)                     | Core data structures for enlace platform configuration.                                |
| [`build`](enlace.build.html.md#module-enlace.build)                   | Run and validate declarative app builds from `app.toml` `[build]`.                     |
| [`compose`](enlace.compose.html.md#module-enlace.compose)               | ASGI app composition for enlace.                                                       |
| [`diagnose`](enlace.diagnose.html.md#module-enlace.diagnose)             | Diagnose app compatibility with enlace.                                                |
| [`discover`](enlace.discover.html.md#module-enlace.discover)             | Convention-based app discovery for enlace.                                             |
| [`doctor`](enlace.doctor.html.md#module-enlace.doctor)                 | Post-deploy smoke checks for a running enlace gateway.                                 |
| [`frontend`](enlace.frontend.html.md#module-enlace.frontend)             | SPA-aware static file serving for enlace.                                              |
| [`gzip_selective`](enlace.gzip_selective.html.md#module-enlace.gzip_selective) | Compression that knows what it must not compress.                                      |
| [`manifest`](enlace.manifest.html.md#module-enlace.manifest)             | Deploy manifest: build-identity for diagnosing "what is actually deployed".            |
| [`proxy`](enlace.proxy.html.md#module-enlace.proxy)                   | Lightweight ASGI reverse proxy for process and external backends.                      |
| [`strategies`](enlace.strategies.html.md#module-enlace.strategies)         | Backend-strategy registry and built-in strategies for enlace.                          |
| [`supervise`](enlace.supervise.html.md#module-enlace.supervise)           | Dev-mode process supervisor for enlace.                                                |
| [`util`](enlace.util.html.md#module-enlace.util)                     | Internal helpers for enlace.                                                           |
