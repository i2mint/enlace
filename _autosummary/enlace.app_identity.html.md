# enlace.app_identity

The app identity checklist: what every public-facing app’s page must carry.

One list, stated once ([`CHECKLIST`](#enlace.app_identity.CHECKLIST)), checked in one place
([`identity_gaps()`](#enlace.app_identity.identity_gaps)) and reported by every surface that audits an app
(`enlace check`, `enlace doctor`, a deploy gate reading the doctor’s JSON).

The platform already wires the favicon, apple-touch-icon and web manifest
`<link>` tags from the app’s icon (see [`enlace.app_icons`](enlace.app_icons.html.md#module-enlace.app_icons)), so those need
only a raster source icon here. What the app’s own page must supply is its
`<head>` text and social-preview tags.

```pycon
>>> sorted(CHECKLIST)[:3]
['description', 'icon-512', 'icon-png']
```

### Module Attributes

| [`MIN_ICON_PX`](#enlace.app_identity.MIN_ICON_PX)   | Smallest PNG source that renders crisply at the largest manifest size.   |
|----------------------------------------------------------------|--------------------------------------------------------------------------|
| [`CHECKLIST`](#enlace.app_identity.CHECKLIST)     | code -> what the app owes (the SSOT; docs and skills point here).        |

### Functions

| [`app_gaps`](#enlace.app_identity.app_gaps)(app, config)   | Checklist codes this one app misses (empty: complete).               |
|--------------------------------------------------------------------------|----------------------------------------------------------------------|
| [`identity_gaps`](#enlace.app_identity.identity_gaps)(config)   | `{app name: missing codes}` for apps whose HTML the platform serves. |

### enlace.app_identity.CHECKLIST *: [dict](https://docs.python.org/3/builtins/stdtypes.html#dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [str](https://docs.python.org/3/builtins/stdtypes.html#str)]* *= {'description': '<meta name="description"> (or description in app.toml)', 'icon-512': 'a PNG source icon >= 512px wide (manifest 512)', 'icon-png': 'a raster icon (platform cuts favicon, apple-touch 180)', 'og-image': '<meta property="og:image"> (social preview PNG)', 'theme-color': '<meta name="theme-color">', 'title': 'a <title> in the page <head>'}*

code -> what the app owes (the SSOT; docs and skills point here).

### enlace.app_identity.MIN_ICON_PX *= 512*

Smallest PNG source that renders crisply at the largest manifest size.

### enlace.app_identity.app_gaps(app, config)

Checklist codes this one app misses (empty: complete).

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### enlace.app_identity.identity_gaps(config)

`{app name: missing codes}` for apps whose HTML the platform serves.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]]
