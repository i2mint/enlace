# enlace.discover

Convention-based app discovery for enlace.

Walks an apps directory, discovers backend entry points and frontend assets,
detects app types, loads per-app TOML overrides, and returns validated AppConfig
objects with provenance tracking.

Discovering an `asgi`-mode app *imports* its entry module, so a single broken
app can take discovery down — and every CLI verb discovers first. The
`on_import_error` policy (`"raise"`, the default and historical behaviour,
or `"record"`) decides which happens: booting must still refuse a broken app,
but the diagnostic verbs need to survive one in order to report it. See
[`discover_apps()`](#enlace.discover.discover_apps).

### Functions

| [`discover_apps`](#enlace.discover.discover_apps)([config, on_import_error])   | High-level discovery: load config, discover apps, check conflicts.   |
|---------------------------------------------------------------------------------------------|----------------------------------------------------------------------|

### Classes

| [`AppDiscoverer`](#enlace.discover.AppDiscoverer)(\*args, \*\*kwargs)        | Protocol for app discovery strategies.    |
|-------------------------------------------------------------------------------------------|-------------------------------------------|
| [`ConventionDiscoverer`](#enlace.discover.ConventionDiscoverer)([conventions, ...]) | Discovers apps by filesystem conventions. |

### *class* enlace.discover.AppDiscoverer(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Protocol for app discovery strategies.

### *class* enlace.discover.ConventionDiscoverer(conventions=None, , on_import_error='raise')

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

### enlace.discover.discover_apps(config=None, , on_import_error='raise')

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
