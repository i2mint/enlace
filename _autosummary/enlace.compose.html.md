# enlace.compose

ASGI app composition for enlace.

Builds a single FastAPI application by mounting discovered sub-apps and applying
cross-cutting middleware. Handles lifespan cascading to mounted sub-apps
(Starlette does not do this natively).

Plugins:
: `build_backend` accepts a `plugins` argument — a sequence of callables
  `(parent: FastAPI, config: PlatformConfig) -> None` invoked once after
  sub-apps are mounted. `enlace_auth.plugin` is the canonical example:
  when installed, it adds auth, sessions, the admin dashboard, and per-user
  stores. enlace itself is auth-agnostic.

### Functions

| [`build_backend`](#enlace.compose.build_backend)(config, \*[, plugins, ...])   | Compose all app backends into a single ASGI application.                |
|----------------------------------------------------------------------------------------------|-------------------------------------------------------------------------|
| [`build_launcher_item`](#enlace.compose.build_launcher_item)(app, config, overlay)   | Build one `/_apps` item: resolved metadata + launchability, for an app. |
| [`create_app`](#enlace.compose.create_app)()                                | App factory for Uvicorn's --factory flag.                               |

### Exceptions

| [`EnlaceConfigError`](#enlace.compose.EnlaceConfigError)   | Raised when platform configuration is unusable at startup.   |
|----------------------------------------------------------------------|--------------------------------------------------------------|

### *exception* enlace.compose.EnlaceConfigError

Bases: [`RuntimeError`](https://docs.python.org/3/builtins/exceptions.html#RuntimeError)

Raised when platform configuration is unusable at startup.

Distinct from `ValueError` / `RuntimeError` so callers and tests can
target this specific class.

### enlace.compose.build_backend(config, , plugins=(), analytics_store=None)

Compose all app backends into a single ASGI application.

For each discovered app:

- mode=asgi, asgi_app: mount the ASGI object at the route prefix
- mode=asgi, functions: build an APIRouter with POST routes and include it
- mode=process/external: mount a reverse proxy at the route prefix
- mode=static: mount StaticFiles at the route prefix
- frontend_only (mode=asgi): skip (no backend to mount)

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

### enlace.compose.build_launcher_item(app, config, overlay)

Build one `/_apps` item: resolved metadata + launchability, for an app.

Public (unlike the `_`-helpers) so the `enlace_auth` plugin’s
metadata-edit endpoints can return the *identical* item shape after a
mutation — single source of truth for the item contract. `overlay` is the
app’s Tier-A record (`{}` if none).

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### enlace.compose.create_app()

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
