# enlace.appmeta

App metadata: harvest, resolve, and render titles / descriptions / keywords / icons.

This module gives every discovered app a *title*, a *description*, a set of
*keywords*, and an *icon* — assembled from the standard places an app already
declares that information, so that a launcher UI can present a searchable grid
of apps without each app having to be hand-registered.

Two things live here:

1. **Harvest + resolve** (`harvest_app_metadata`) — at *discovery* time, read the
   app-declared metadata from, in precedence order, `app.toml` → a Web App
   Manifest → the `index.html` `<head>` → `package.json` → `pyproject.toml`
   → filesystem icon conventions, and bake the resolved values onto the
   `AppConfig`. Scalars are first-non-empty-wins; keywords are *unioned* across
   every source (so app-declared keywords accumulate rather than overwrite).
2. **Icon rendering** (`resolve_icon`) — at *request* time, turn an app’s icon
   spec (an emoji, a relative image path, a `data:` URI, an absolute URL, or
   nothing) into bytes to serve (or a redirect). An unset icon falls back to a
   deterministic, font-guaranteed letter *monogram* on a name-hashed gradient, so
   a grid of many apps reads as one coherent system rather than a wall of
   placeholders.

Layering note: this module is pure read-model. The *editable* overlay (a
runtime store of owner-added keywords / icon overrides) and its HTTP write
surface live in the `enlace_auth` plugin, not here — enlace core never mutates
metadata. Core only *reads* an injected overlay mapping via `request.app.state`
(see `enlace.compose`).

### Functions

| [`app_dir_of`](#enlace.appmeta.app_dir_of)(config)                           | Reconstruct an app's own directory from its `AppConfig`.                      |
|-----------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------|
| [`effective_icon_spec`](#enlace.appmeta.effective_icon_spec)(config, \*[, ...])       | The icon spec to render, applying Tier A (overlay) over C/D, then default.    |
| [`harvest_app_metadata`](#enlace.appmeta.harvest_app_metadata)(config, app_dir, ...)   | Resolve app-declared metadata for one app; return an `AppConfig` update dict. |
| [`resolve_icon`](#enlace.appmeta.resolve_icon)(spec, \*, app_name, ...[, ...]) | Turn an icon spec into servable bytes (or a redirect) plus a cache token.     |
| [`resolve_keywords`](#enlace.appmeta.resolve_keywords)(\*, app_keywords, ...)      | Union app / platform / overlay keywords; return (merged, sources).            |

### Classes

| [`AppMetaConfig`](#enlace.appmeta.AppMetaConfig)(\*\*data)                      | The `[app_meta]` table of `platform.toml`.                               |
|-----------------------------------------------------------------------------------------------|--------------------------------------------------------------------------|
| [`AppMetaEntry`](#enlace.appmeta.AppMetaEntry)(\*\*data)                       | Per-app static override from `platform.toml` (`[app_meta.apps.<name>]`). |
| [`IconResult`](#enlace.appmeta.IconResult)(content_type, token[, body, ...]) | The outcome of resolving an icon spec.                                   |

### *class* enlace.appmeta.AppMetaConfig(\*\*data)

Bases: `BaseModel`

The `[app_meta]` table of `platform.toml`.

`default_icon` and `apps` are consumed by enlace core (read model).
`editors` and `store_path` are *held but not acted on* by core — the
`enlace_auth` plugin reads them to gate and persist the runtime overlay,
mirroring how `PlatformConfig.auth` / `.stores` are carried for the
plugin without core interpreting them.

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.appmeta.AppMetaEntry(\*\*data)

Bases: `BaseModel`

Per-app static override from `platform.toml` (`[app_meta.apps.<name>]`).

The platform owner’s static layer, applied on top of app-declared metadata.
`keywords` here are *added* to the app’s own (union), not replaced; the
scalar fields override first-non-empty-wins if set.

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.appmeta.IconResult(content_type, token, body=None, redirect_url=None, immutable=True)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

The outcome of resolving an icon spec.

Exactly one of (`body`, `redirect_url`) is set. `token` is a short
content hash used as the `?v=` cache-buster and the `ETag`. `immutable`
is False only for the redirect form (whose target we don’t control).

### enlace.appmeta.app_dir_of(config)

Reconstruct an app’s own directory from its `AppConfig`.

`source_dir` is the *container* dir and `name` the subdir, for both the
walked-container and individual-app-dir discovery paths — so `source_dir /
name` is the app dir in both cases.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)]

### enlace.appmeta.effective_icon_spec(config, , overlay_icon=None, default_icon='')

The icon spec to render, applying Tier A (overlay) over C/D, then default.

Empty string means “no explicit icon” — the renderer produces a monogram.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### enlace.appmeta.harvest_app_metadata(config, app_dir, frontend_dir)

Resolve app-declared metadata for one app; return an `AppConfig` update dict.

Called at the tail of discovery for *both* the asgi and non-asgi paths, so
external/process apps (which have no frontend on disk) are covered too. Every
harvester is a no-op when its source file is missing, so an app with only an
`app.toml` — or nothing — resolves cleanly without raising.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

Precedence (first non-empty wins for scalars; keywords union across all):
: Tier C.1 app.toml (already on `config`, gated by provenance for the title)
  Tier C.2 manifest → C.3 <head> → C.4 package.json → C.5 pyproject → C.6 fs
  Tier D   derived (title = auto-derived name; icon = “” ⇒ monogram at render)

Returns only the fields that changed, suitable for `model_copy(update=...)`.

### enlace.appmeta.resolve_icon(spec, , app_name, display_name, app_dir, token_only=False)

Turn an icon spec into servable bytes (or a redirect) plus a cache token.

Dispatch order: explicit `emoji:` → `data:` URI → absolute https URL
(redirect) → contained image file → bare glyph → monogram fallback. Any spec
that fails its form’s validation falls through to the monogram, so the
endpoint never errors and never serves an unexpected file type.

`token_only=True` skips reading file bytes (the `?v=` token for a file is
stat-derived, so it matches the full resolve either way). `/_apps` uses it
to avoid reading every app’s icon file on every listing; the icon endpoint
uses the full form to get the bytes to serve.

* **Return type:**
  [`IconResult`](#enlace.appmeta.IconResult)

### enlace.appmeta.resolve_keywords(, app_keywords, platform_keywords, overlay_keywords)

Union app / platform / overlay keywords; return (merged, sources).

`merged` is what search matches on. `sources` *partitions* `merged`:
each keyword appears in exactly one bucket — the first (highest-precedence)
tier that declared it, in order app → platform → overlay. This keeps the
UI’s read-only-vs-editable split unambiguous: a keyword the app already
declares stays in the read-only `app` bucket even if the owner also typed
it into the overlay, so it isn’t offered as a removable overlay chip
(removing it wouldn’t change `merged` anyway).

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)], [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)]
