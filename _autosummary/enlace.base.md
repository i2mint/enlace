# enlace.base

Core data structures for enlace platform configuration.

enlace does not enforce access — that’s `enlace_auth`’s job. But enlace
*does* read `AppConfig.access` and `AppConfig.allowed_users` for one
narrow purpose: filtering the `/_apps` listing so authenticated users
don’t see entries they couldn’t open anyway. That makes the access string
vocabulary part of enlace’s contract — the values
`"public" | "local" | "protected:shared" | "protected:user"` are the
ones `enlace.access.can_see_app` understands; anything else is treated as
deny-by-default.

The fields are otherwise opaque to enlace: enforcement, session lookup,
allowlist matching at request time, and CSRF all live in the
`enlace_auth` plugin (passed in via `build_backend(..., plugins=[...])`).

Likewise, `[auth.*]` and `[stores.*]` tables in `platform.toml` are
preserved as untyped dicts on `PlatformConfig` so plugins can deserialize
them with their own models.

### Classes

| [`AppConfig`](#enlace.base.AppConfig)(\*\*data)         | Resolved configuration for a single discovered app.                |
|------------------------------------------------------------------------------|--------------------------------------------------------------------|
| [`AppImportError`](#enlace.base.AppImportError)(\*\*data)    | Why an app's entry module could not be imported at discovery time. |
| [`BuildConfig`](#enlace.base.BuildConfig)(\*\*data)       | Declarative build instructions for an app's compiled frontend.     |
| [`ConventionsConfig`](#enlace.base.ConventionsConfig)(\*\*data) | Meta-conventions controlling how apps are discovered.              |
| [`PlatformConfig`](#enlace.base.PlatformConfig)(\*\*data)    | Resolved configuration for the entire platform.                    |

### *class* enlace.base.AppConfig(\*\*data)

Bases: `BaseModel`

Resolved configuration for a single discovered app.

`extra="allow"` lets plugin strategies (e.g. `enlace_docker`) carry
their own typed fields — `dockerfile`, `image`, `compose_file`,
`service`, etc. — without enlace having to enumerate them. The
plugin’s `BackendStrategy` reads them via attribute access
(`app.dockerfile`); enlace itself ignores them.

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {'extra': 'allow'}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.base.AppImportError(\*\*data)

Bases: `BaseModel`

Why an app’s entry module could not be imported at discovery time.

Recorded (instead of raised) when discovery runs with
`on_import_error="record"` — see [`enlace.discover.discover_apps()`](enlace.discover.md#enlace.discover.discover_apps).
Importing a module runs arbitrary module-level code, so `exception_type`
is not necessarily an `ImportError`: the failure seen in production was a
`PermissionError` from a dependency reading a root-only dotenv at import.

#### *classmethod* from_exception(exc, entry_module_path=None)

Build a record from the exception the import raised.

* **Return type:**
  [`AppImportError`](#enlace.base.AppImportError)

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.base.BuildConfig(\*\*data)

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

### *class* enlace.base.ConventionsConfig(\*\*data)

Bases: `BaseModel`

Meta-conventions controlling how apps are discovered.

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.base.PlatformConfig(\*\*data)

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
  [`PlatformConfig`](#enlace.base.PlatformConfig)

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].
