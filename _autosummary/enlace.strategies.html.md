# enlace.strategies

Backend-strategy registry and built-in strategies for enlace.

A `BackendStrategy` owns everything mode-specific about an app:

- which `app.toml` keys it understands (`toml_field_map` / `path_keys`);
- whether discovery should skip Python introspection
  (`skip_python_introspection`);
- mode-specific config validation (`validate`);
- how to expose the app over HTTP (`make_asgi` — returns an ASGI sub-app
  to mount, `None` for no HTTP);
- how to run the app as a managed process (`make_lifecycle` — returns a
  `Lifecycle` to supervise, `None` for no process).

This module is the **open/closed extension point** for new backend modes.
The four built-in strategies (`asgi`, `process`, `external`,
`static`) capture today’s behavior verbatim. External plugins like
`enlace_docker` register additional strategies via the
`enlace.backend_strategies` entry-point group; installing the plugin
package is enough — no user code change required.

Registry semantics:

- `register_strategy` is idempotent on `name` collisions: the latest
  registration wins (so plugins can override built-ins if they really must).
- `get_strategy` performs a *lazy* entry-point scan on cache miss, so
  tests and direct callers don’t need to import plugin packages manually.
- The four built-ins are registered eagerly at module import.

### Functions

| [`collect_strategy_field_maps`](#enlace.strategies.collect_strategy_field_maps)()   | Aggregate `toml_field_map` and `path_keys` across all strategies.          |
|----------------------------------------------------------------------------------|----------------------------------------------------------------------------|
| [`get_strategy`](#enlace.strategies.get_strategy)(name)              | Return the strategy registered for `name`.                                 |
| [`iter_strategies`](#enlace.strategies.iter_strategies)()               | Return all registered strategies (after entry-point scan), sorted by name. |
| [`known_modes`](#enlace.strategies.known_modes)()                   | Return a sorted list of registered mode names (after entry-point scan).    |
| [`register_strategy`](#enlace.strategies.register_strategy)(strategy)     | Register a backend strategy under its `name`.                              |

### Classes

| [`AsgiStrategy`](#enlace.strategies.AsgiStrategy)()                | Default mode: import the app's Python module and mount its ASGI callable.   |
|--------------------------------------------------------------------------------|-----------------------------------------------------------------------------|
| [`BackendStrategy`](#enlace.strategies.BackendStrategy)()             | Base class for backend strategies.                                          |
| [`ExternalStrategy`](#enlace.strategies.ExternalStrategy)()            | Route to a pre-existing service at a known URL; no lifecycle.               |
| [`Lifecycle`](#enlace.strategies.Lifecycle)(\*args, \*\*kwargs) | Protocol for an enlace-supervised backend.                                  |
| [`ProcessStrategy`](#enlace.strategies.ProcessStrategy)()             | Spawn the app as a child process, health-check, restart, proxy.             |
| [`StaticStrategy`](#enlace.strategies.StaticStrategy)()              | Serve a directory of static files (no Python, no proxy).                    |

### *class* enlace.strategies.AsgiStrategy

Bases: [`BackendStrategy`](#enlace.strategies.BackendStrategy)

Default mode: import the app’s Python module and mount its ASGI callable.

Also covers the `functions` app_type (auto-generated FastAPI routes
wrapping the module’s typed public functions) and `frontend_only`
(no backend to mount — returns `None` from `make_asgi`).

#### make_asgi(app, platform)

Build the ASGI sub-app to mount at `app.route_prefix`.

Return `None` if this strategy contributes no HTTP routing (e.g.
an asgi-mode `frontend_only` app).

### *class* enlace.strategies.BackendStrategy

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Base class for backend strategies.

Subclasses set `name` and override the methods relevant to their mode.
Concrete subclasses for the built-in modes live below; external plugins
subclass this and register their instance via `register_strategy` or
the `enlace.backend_strategies` entry-point group.

#### make_asgi(app, platform)

Build the ASGI sub-app to mount at `app.route_prefix`.

Return `None` if this strategy contributes no HTTP routing (e.g.
an asgi-mode `frontend_only` app).

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Callable`](https://docs.python.org/3/library/typing.html#typing.Callable)[[`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]]

#### make_lifecycle(app, platform)

Build the supervised lifecycle for this app, or `None`.

Returning `None` means enlace does not manage a process for this
app (e.g. `asgi`, `external`, `static`).

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Lifecycle`](#enlace.strategies.Lifecycle)]

#### validate(app)

Validate mode-specific fields. Raise `ValueError` if invalid.

Called from `AppConfig` Pydantic validators, so raising
`ValueError` will be wrapped in a `ValidationError` automatically.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### *class* enlace.strategies.ExternalStrategy

Bases: [`BackendStrategy`](#enlace.strategies.BackendStrategy)

Route to a pre-existing service at a known URL; no lifecycle.

#### make_asgi(app, platform)

Build the ASGI sub-app to mount at `app.route_prefix`.

Return `None` if this strategy contributes no HTTP routing (e.g.
an asgi-mode `frontend_only` app).

#### validate(app)

Validate mode-specific fields. Raise `ValueError` if invalid.

Called from `AppConfig` Pydantic validators, so raising
`ValueError` will be wrapped in a `ValidationError` automatically.

### *class* enlace.strategies.Lifecycle(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Protocol for an enlace-supervised backend.

The dev-mode supervisor (`enlace.supervise.supervise_all`) drives any
object satisfying this protocol. `ManagedProcess` in `supervise.py`
is the canonical implementation for subprocess-backed apps; plugins
(e.g. `enlace_docker`) may provide other implementations (e.g. a
container-backed lifecycle that shells out to `docker`).

The supervisor calls (in order, per attempt):
`start()` → `stream_logs()` (concurrently with) `wait_healthy()`
→ `wait_exit()` → `should_restart()` → `backoff_delay()`.

Restart accounting (`record_failure`, `maybe_reset_backoff`) is
handled by the lifecycle so each implementation can choose its own
policy semantics.

### *class* enlace.strategies.ProcessStrategy

Bases: [`BackendStrategy`](#enlace.strategies.BackendStrategy)

Spawn the app as a child process, health-check, restart, proxy.

#### make_asgi(app, platform)

Build the ASGI sub-app to mount at `app.route_prefix`.

Return `None` if this strategy contributes no HTTP routing (e.g.
an asgi-mode `frontend_only` app).

#### make_lifecycle(app, platform)

Build the supervised lifecycle for this app, or `None`.

Returning `None` means enlace does not manage a process for this
app (e.g. `asgi`, `external`, `static`).

#### validate(app)

Validate mode-specific fields. Raise `ValueError` if invalid.

Called from `AppConfig` Pydantic validators, so raising
`ValueError` will be wrapped in a `ValidationError` automatically.

### *class* enlace.strategies.StaticStrategy

Bases: [`BackendStrategy`](#enlace.strategies.BackendStrategy)

Serve a directory of static files (no Python, no proxy).

#### make_asgi(app, platform)

Build the ASGI sub-app to mount at `app.route_prefix`.

Return `None` if this strategy contributes no HTTP routing (e.g.
an asgi-mode `frontend_only` app).

#### validate(app)

Validate mode-specific fields. Raise `ValueError` if invalid.

Called from `AppConfig` Pydantic validators, so raising
`ValueError` will be wrapped in a `ValidationError` automatically.

### enlace.strategies.collect_strategy_field_maps()

Aggregate `toml_field_map` and `path_keys` across all strategies.

Used by `discover._overlay_toml_fields` to know which TOML keys can
map to which AppConfig fields. The core TOML keys (`route`,
`access`, `display_name`, `frontend_dir`, `mode`, etc.) live in
discover.py’s own map; this only contributes mode-specific keys.

Collisions between strategies on the same TOML key are resolved
last-write-wins (in registration order). In practice the built-ins
don’t collide, and plugins are expected to use distinct keys.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)], [`set`](https://docs.python.org/3/builtins/stdtypes.html#set)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]]

### enlace.strategies.get_strategy(name)

Return the strategy registered for `name`.

On cache miss, performs a lazy scan of the `enlace.backend_strategies`
entry-point group so plugins are discovered without an explicit import.
Raises `ValueError` with an actionable message if the mode is unknown
after the scan.

* **Return type:**
  [`BackendStrategy`](#enlace.strategies.BackendStrategy)

### enlace.strategies.iter_strategies()

Return all registered strategies (after entry-point scan), sorted by name.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`BackendStrategy`](#enlace.strategies.BackendStrategy)]

### enlace.strategies.known_modes()

Return a sorted list of registered mode names (after entry-point scan).

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### enlace.strategies.register_strategy(strategy)

Register a backend strategy under its `name`.

Idempotent on collisions: a later registration replaces an earlier one.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)
