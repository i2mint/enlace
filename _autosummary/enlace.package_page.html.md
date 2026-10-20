# enlace.package_page

Serve an app’s page from its Python package: the shell and its `/ui/` assets.

Some apps ship their whole page inside their Python package: a script and a
stylesheet (and maybe vendored files) under a data directory, plus a tiny HTML
shell that tells the script where its API lives. Before this module, the host
committed that generated shell as the app’s `frontend/index.html` and the app
served its own assets from a hand-written `/ui/{name}` route. Now one app.toml
key does both, and a UI change ships by upgrading the package:

```default
page_from_package = "mypkg"     # assets in mypkg/data/ui, generic shell

[page_from_package]             # or the full table
package = "songleaf"
assets = "data/web"             # directory inside the package
shell = "songleaf.web:page_shell"   # the app's own shell, if it has one
```

What enlace serves for such an app (`name`, API at `route_prefix`):

- `/{name}/` and every path under it: the shell (client-side routing), with
  `Cache-Control: no-cache` and an `ETag`, so a package upgrade reaches
  the browser on its next load.
- `{route_prefix}/ui/<file>`: the files of the package’s assets directory,
  always revalidated. Mounted ahead of the app; what it does not serve falls
  through to the app, so the app’s own `/ui` routes keep working.

The shell is the app’s own (`shell = "module:function"`, called as
`function(api=..., base=..., title=...)` and returning HTML) or enlace’s
generic one ([`page_shell()`](#enlace.package_page.page_shell)): `window.<GLOBAL> = {api, base, title}` and
the `scripts` and `stylesheets`, loaded from the assets route.

The app does not change and does not import enlace. Run alone, it keeps
serving its own page however it already did.

The package is imported into the gateway, as an `asgi` app is. A failure to
import it, find its assets directory or build the shell is a load failure of
the app (see [`enlace.degraded`](enlace.degraded.html.md#module-enlace.degraded)): loud, and local to that app.

### Module Attributes

| [`DFLT_ASSETS_DIR`](#enlace.package_page.DFLT_ASSETS_DIR)    | Where the assets live inside the package, by default.                    |
|---------------------------------------------------------------------|--------------------------------------------------------------------------|
| [`DFLT_ASSETS_ROUTE`](#enlace.package_page.DFLT_ASSETS_ROUTE)  | The route segment, under the app's API prefix, the assets are served at. |
| [`DFLT_CACHE_CONTROL`](#enlace.package_page.DFLT_CACHE_CONTROL) | keep, but revalidate (a cheap 304).                                      |
| [`ShellFunc`](#enlace.package_page.ShellFunc)          | what a shell function is called with.                                    |

### Functions

| [`page_shell`](#enlace.package_page.page_shell)(\*, api[, base, title, ...])            | The generic shell: `window.<global_name> = {api, base, title}`, then assets.   |
|-----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------|
| [`resolve_package_page`](#enlace.package_page.resolve_package_page)(config, \*, api, base, title) | Import the package, find its assets directory and build the shell.             |

### Classes

| [`PackageAssets`](#enlace.package_page.PackageAssets)(\*args[, allowed, ...])         | A package's page files, always revalidated, optionally allowlisted.          |
|------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| [`PackagePage`](#enlace.package_page.PackagePage)(shell, assets_dir[, ...])         | A resolved package page: the shell's HTML and the directory of its assets.   |
| [`PackagePageConfig`](#enlace.package_page.PackagePageConfig)(\*, package[, assets, ...]) | app.toml's `page_from_package`: the package that ships the app's page.       |
| [`ShellApp`](#enlace.package_page.ShellApp)(shell, \*[, cache_control])          | Pure-ASGI app answering every GET/HEAD with the shell (client-side routing). |

### enlace.package_page.DFLT_ASSETS_DIR *= 'data/ui'*

Where the assets live inside the package, by default.

### enlace.package_page.DFLT_ASSETS_ROUTE *= 'ui'*

The route segment, under the app’s API prefix, the assets are served at.

### enlace.package_page.DFLT_CACHE_CONTROL *= 'no-cache'*

keep, but revalidate (a cheap 304).

* **Type:**
  Cache policy of the shell and the assets

### *class* enlace.package_page.PackageAssets(\*args, allowed=(), cache_control='no-cache', fallback=None, route='ui', \*\*kwargs)

Bases: `StaticFiles`

A package’s page files, always revalidated, optionally allowlisted.

Hidden files (a path segment starting with `.`) and `__pycache__` are
never served. A request it does not serve (no such file, not on the
allowlist, a method other than GET/HEAD, a websocket) goes to
`fallback` when one is given (the app’s own mount, so a route the app
has under the same segment keeps working), else gets a 404 / 405.
`fallback` sees the scope the app’s mount would have given it: the
assets route segment moves from `root_path` back into the path.

#### file_response(\*args, \*\*kwargs)

The file, with `Cache-Control` so a package upgrade reaches the browser.

* **Return type:**
  `Response`

#### *async* get_response(path, scope)

Refuse hidden files and, with an allowlist, any file not on it.

* **Return type:**
  `Response`

### *class* enlace.package_page.PackagePage(shell, assets_dir, assets_route='ui', files=tuple())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A resolved package page: the shell’s HTML and the directory of its assets.

#### assets_app(, fallback=None)

The ASGI app serving the assets directory; misses go to `fallback`.

* **Return type:**
  [`PackageAssets`](#enlace.package_page.PackageAssets)

#### shell_app()

The ASGI app serving the shell at every path under the page’s mount.

* **Return type:**
  [`ShellApp`](#enlace.package_page.ShellApp)

### *class* enlace.package_page.PackagePageConfig(\*, package, assets='data/ui', assets_route='ui', files=<factory>, shell=None, global_name='', title='', scripts=<factory>, stylesheets=<factory>)

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

### *class* enlace.package_page.ShellApp(shell, , cache_control='no-cache')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Pure-ASGI app answering every GET/HEAD with the shell (client-side routing).

`Cache-Control: no-cache` plus a strong `ETag`, so a browser revalidates
with a cheap 304 and picks up a new shell after a package upgrade.

### enlace.package_page.ShellFunc

what a shell function is called with.

* **Type:**
  `(*, api, base, title) -> html`

alias of `Callable`[[…], [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### enlace.package_page.page_shell(, api, base='', title='', global_name='APP', scripts=('app.js',), stylesheets=('style.css',), assets_route='ui')

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

### enlace.package_page.resolve_package_page(config, , api, base, title)

Import the package, find its assets directory and build the shell.

Raises (`ImportError`, `FileNotFoundError`, `TypeError`…) when the
package, its assets directory or the shell function is not usable; the
caller decides whether that stubs the app or stops the boot.

* **Return type:**
  [`PackagePage`](#enlace.package_page.PackagePage)
