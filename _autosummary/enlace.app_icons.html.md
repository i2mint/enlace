# enlace.app_icons

One icon per app, served in every form a browser or phone asks for.

An app’s icon appears in four places: the launcher grid, the browser tab
(favicon), the iOS home screen (`apple-touch-icon`, PNG only) and the Android
/ Chrome home screen (the web-app manifest’s `icons`). Left to each app, those
four drift apart — most apps declare none of them, and the few that do declare
different subsets. So the platform owns them, once:

- **Source** — [`icon_source()`](#enlace.app_icons.icon_source) picks one icon spec per app, in tier order:
  runtime overlay (Tier A) > `[app_meta.apps.<name>].icon` (Tier B) > a file
  named `<name>.<ext>` in `[app_meta].icons_dir` (Tier B, by convention) >
  the app’s own declared/harvested icon (Tier C) > `default_icon` > monogram.
- **Renditions** — `/_apps/{name}/icon` serves the source as-is;
  `/_apps/{name}/icon-{size}.png` serves square PNGs (`PNG_SIZES`) cut
  from a *raster* source; `/_apps/{name}/manifest.webmanifest` is generated.
- **Wiring** — [`AppIconLinksMiddleware`](#enlace.app_icons.AppIconLinksMiddleware) adds the `<link>` tags an app’s
  HTML is missing. When the icon comes from the platform tiers (A/B), the page’s
  own icon links are replaced, so the owner’s choice wins everywhere; otherwise
  the app’s own links stand and only the gaps are filled.

PNG renditions need Pillow and a raster source (PNG/JPEG/WebP/GIF/ICO). A
vector-only icon (SVG, emoji glyph, monogram) still serves as the favicon, but
has no PNG form — so no home-screen icon. Give such an app a PNG in
`icons_dir`; [`home_screen_gaps()`](#enlace.app_icons.home_screen_gaps) lists them.

### Functions

| [`display_name_of`](#enlace.app_icons.display_name_of)(app, config, overlay)             | The app's display name across Tier A / B / C (same order as `/_apps`).                                             |
|----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| [`has_own_pages`](#enlace.app_icons.has_own_pages)(app)                                | Whether the platform serves this app's HTML itself (so it can wire its head).                                      |
| [`head_links`](#enlace.app_icons.head_links)(app, \*, icon, display_name, ...)      | Return `(snippet, head)`: tags to add, and the head minus replaced ones.                                           |
| [`home_screen_gaps`](#enlace.app_icons.home_screen_gaps)(config)                          | Names of launchable apps whose icon has no PNG form (no home-screen icon).                                         |
| [`icon_source`](#enlace.app_icons.icon_source)(app, config, overlay)                 | Return `(spec, root, platform_owned)` for one app's icon.                                                          |
| [`icons_dir_file`](#enlace.app_icons.icons_dir_file)(icons_dir, name)                   | The filename of `<name>.<ext>` in the platform icons dir, if present.                                              |
| [`is_raster`](#enlace.app_icons.is_raster)(icon)                                   | Whether an icon can be cut into PNG renditions (a raster, served inline).                                          |
| [`manifest_json`](#enlace.app_icons.manifest_json)(manifest)                           | Serialize a manifest (kept apart so routes and tests share one encoding).                                          |
| [`overlay_entry`](#enlace.app_icons.overlay_entry)(scope, name)                        | Read one app's runtime overlay record (Tier A), or `{}` if none.                                                   |
| [`png_rendition`](#enlace.app_icons.png_rendition)(body, size)                         | A `size``×``size` PNG cut from raster `body`, or None if impossible.                                               |
| [`resolve_app_icon`](#enlace.app_icons.resolve_app_icon)(app, config, overlay, \*[, ...]) | Resolve an app's icon to an [`IconResult`](enlace.appmeta.html.md#enlace.appmeta.IconResult). |
| [`web_manifest`](#enlace.app_icons.web_manifest)(app, \*, display_name, ...)          | The web-app manifest for one app (what Android's "Add to Home screen" reads).                                      |

### Classes

| [`AppIconLinksMiddleware`](#enlace.app_icons.AppIconLinksMiddleware)(app, \*, config, ...)   | Pure-ASGI middleware giving every app's HTML the same icon wiring.   |
|-------------------------------------------------------------------------------------------------|----------------------------------------------------------------------|

### *class* enlace.app_icons.AppIconLinksMiddleware(app, , config, is_protected)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Pure-ASGI middleware giving every app’s HTML the same icon wiring.

For `text/html` responses under `/{app}/`, see [`head_links()`](#enlace.app_icons.head_links). The
overlay (Tier A) is read per request from `app.state.app_meta_overlay`, so
a live icon edit in the launcher reaches tabs and home screens with no
redeploy. Must sit inside compression (it edits the body).

### enlace.app_icons.display_name_of(app, config, overlay)

The app’s display name across Tier A / B / C (same order as `/_apps`).

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### enlace.app_icons.has_own_pages(app)

Whether the platform serves this app’s HTML itself (so it can wire its head).

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### enlace.app_icons.head_links(app, , icon, display_name, protected, page_head, replace)

Return `(snippet, head)`: tags to add, and the head minus replaced ones.

Adds each of favicon / apple-touch-icon / manifest / home-screen title only
where the page lacks it — unless `replace`, in which case the page’s own
icon links are dropped and the platform’s take their place.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes), [`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes)]

### enlace.app_icons.home_screen_gaps(config)

Names of launchable apps whose icon has no PNG form (no home-screen icon).

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### enlace.app_icons.icon_source(app, config, overlay)

Return `(spec, root, platform_owned)` for one app’s icon.

`root` is the directory a file spec is resolved (and contained) under.
`platform_owned` is True when the icon comes from the platform tiers
(overlay, `[app_meta.apps]`, `icons_dir`) rather than the app itself —
the signal that the page’s own icon links should be replaced.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)], [`bool`](https://docs.python.org/3/builtins/functions.html#bool)]

### enlace.app_icons.icons_dir_file(icons_dir, name)

The filename of `<name>.<ext>` in the platform icons dir, if present.

Raster formats are probed before SVG, so an app with both gets PNG
renditions (the SVG can sit beside it as the editable source).

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### enlace.app_icons.is_raster(icon)

Whether an icon can be cut into PNG renditions (a raster, served inline).

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### enlace.app_icons.manifest_json(manifest)

Serialize a manifest (kept apart so routes and tests share one encoding).

* **Return type:**
  [`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes)

### enlace.app_icons.overlay_entry(scope, name)

Read one app’s runtime overlay record (Tier A), or `{}` if none.

`app_meta_overlay` is injected on the root app’s state by the
`enlace_auth` plugin; absent it, core degrades to Tiers B/C/D.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### enlace.app_icons.png_rendition(body, size)

A `size``×``size` PNG cut from raster `body`, or None if impossible.

Non-square sources are center-cropped (a home-screen tile is square, and
padding would add a frame the artwork never had). Transparency is kept for
the favicon but flattened onto white for the larger sizes, because iOS
renders a transparent apple-touch-icon’s clear pixels as black.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes)]

### enlace.app_icons.resolve_app_icon(app, config, overlay, , token_only=False)

Resolve an app’s icon to an [`IconResult`](enlace.appmeta.html.md#enlace.appmeta.IconResult).

### enlace.app_icons.web_manifest(app, , display_name, description, token, has_png, display)

The web-app manifest for one app (what Android’s “Add to Home screen” reads).

Scoped to the app’s own mount so each app installs as its own shortcut.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)
