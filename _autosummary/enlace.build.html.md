# enlace.build

Run and validate declarative app builds from `app.toml` `[build]`.

enlace owns the `[build]` *contract* (the schema in
[`enlace.base.BuildConfig`](enlace.base.html.md#enlace.base.BuildConfig)) so any deployer can rely on it, and
provides an explicit `enlace build` step that runs it. Builds never run at
request time — production should pre-build before the gateway starts. This
module is the small, dependency-free runner behind that step.

The runner shells out to whatever `install` / `build` commands the app
declares (npm, pnpm, vite, …). enlace does not assume a toolchain; it just
runs the argv the app provides, in the resolved working directory, with the
caller’s environment plus any injected overrides.

### Functions

| [`app_dir_of`](#enlace.build.app_dir_of)(app)                                 | Best-effort resolution of an app's own directory.                    |
|--------------------------------------------------------------------------------------------------|----------------------------------------------------------------------|
| [`build_cwd`](#enlace.build.build_cwd)(app)                                  | The directory build commands run from: `[build].cwd` or the app dir. |
| [`has_build`](#enlace.build.has_build)(app)                                  | Whether the app declares a runnable build (a `build` command).       |
| [`run_build`](#enlace.build.run_build)(app, \*[, extra_env, dry_run, check]) | Run an app's `install` (if any) then `build` commands.               |
| [`validate_build`](#enlace.build.validate_build)(app)                             | Return human-readable problems with an app's `[build]` config.       |

### Classes

| [`BuildResult`](#enlace.build.BuildResult)(app, cwd[, commands, ran, ...])   | Outcome of building one app.   |
|------------------------------------------------------------------------------------------------|--------------------------------|

### *class* enlace.build.BuildResult(app, cwd, commands=<factory>, ran=False, returncode=0)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Outcome of building one app.

### enlace.build.app_dir_of(app)

Best-effort resolution of an app’s own directory.

Used as the default build working directory when `[build].cwd` is not
set. Works for both container-discovered apps (`source_dir/name`) and
directly-discovered ones, and falls back to the entry module’s parent.

* **Return type:**
  [`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)

### enlace.build.build_cwd(app)

The directory build commands run from: `[build].cwd` or the app dir.

* **Return type:**
  [`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)

### enlace.build.has_build(app)

Whether the app declares a runnable build (a `build` command).

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### enlace.build.run_build(app, , extra_env=None, dry_run=False, check=True)

Run an app’s `install` (if any) then `build` commands.

* **Parameters:**
  * **app** ([`AppConfig`](enlace.base.html.md#enlace.base.AppConfig)) – The app whose `[build]` section to run.
  * **extra_env** ([`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]]) – Values to overlay on the inherited environment (e.g. the
    deployer’s `VITE_API_BASE=/api/{name}`). enlace itself does not
    choose values; callers do.
  * **dry_run** ([`bool`](https://docs.python.org/3/builtins/functions.html#bool)) – Resolve and record the commands without executing them.
  * **check** ([`bool`](https://docs.python.org/3/builtins/functions.html#bool)) – Raise `subprocess.CalledProcessError` on nonzero exit.
* **Return type:**
  [`BuildResult`](#enlace.build.BuildResult)
* **Returns:**
  A [`BuildResult`](#enlace.build.BuildResult) describing what was (or would be) run.

### enlace.build.validate_build(app)

Return human-readable problems with an app’s `[build]` config.

Pure validation — never runs commands. Checks that the section, if
present, has a `build` command and that its working directory exists.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]
