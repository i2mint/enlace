# enlace.manifest

Deploy manifest: build-identity for diagnosing “what is actually deployed”.

A deploy manifest answers two diagnostic questions that are otherwise hard to
answer about a running deployment:

1. Is the server really serving the SHA I think it is, or has the checkout
   drifted? (Including externals — editable installs of sibling packages.)
2. Is the page my browser rendered the **current** build, or is the browser
   serving a stale cache?

enlace owns the **schema** and the **read paths** (an HTTP endpoint per app,
a platform-level endpoint, response headers). The **write path** is the
deploy tool’s responsibility: snapshot identity at deploy time and drop a
`{manifest_dir}/{app}.json` file enlace can read. Snapshotting at deploy
time is load-bearing — it captures *what was actually deployed*, not whatever
the working tree on the server happens to say at startup.

The schema is versioned (`schema_version`) so future tooling that consumes
manifests across many apps can evolve without ambiguity.

The endpoint and header layer are always-on and cheap (a few hundred bytes
per app, one route per app, one short header tuple per response). They’re
hidden from end users by default — apps can opt to surface the data visibly
(footer chip, About modal, devtools log) on top of the primitive.

See [https://github.com/i2mint/enlace/issues/18](https://github.com/i2mint/enlace/issues/18) for design rationale.

### Module Attributes

| [`MANIFEST_ERROR_KEY`](#enlace.manifest.MANIFEST_ERROR_KEY)   | `extra` key naming why an on-disk manifest was not used.   |
|-----------------------------------------------------------------------|------------------------------------------------------------|

### Functions

| [`load_manifest`](#enlace.manifest.load_manifest)(app_name, manifest_dir, \*[, ...])   | Load the deploy manifest for one app, or return a minimal stub.   |
|-----------------------------------------------------------------------------------------------------|-------------------------------------------------------------------|
| [`load_platform_manifest`](#enlace.manifest.load_platform_manifest)(manifest_dir, \*[, ...])    | Load the platform-level deploy manifest (or a minimal stub).      |
| [`resolve_manifest_dir`](#enlace.manifest.resolve_manifest_dir)([config_manifest_dir])        | Resolve the manifest directory from env var or config.            |

### Classes

| [`DeployHeadersMiddleware`](#enlace.manifest.DeployHeadersMiddleware)(app, \*, ...[, ...])   | Pure-ASGI middleware that adds X-Deploy-\* headers on every response.       |
|-------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------|
| [`DeployManifest`](#enlace.manifest.DeployManifest)(\*\*data)                       | What was deployed for one app (or the platform itself).                     |
| [`DeployMetaTagMiddleware`](#enlace.manifest.DeployMetaTagMiddleware)(app, \*, ...[, ...])   | Pure-ASGI middleware that injects deploy `<meta>` tags into HTML.           |
| [`ExternalRef`](#enlace.manifest.ExternalRef)(\*\*data)                          | Identity for an externally-installed dependency (e.g. an editable sibling). |
| [`SourceRef`](#enlace.manifest.SourceRef)(\*\*data)                            | Git identity for a single source tree (app or platform).                    |

### *class* enlace.manifest.DeployHeadersMiddleware(app, , manifests_by_prefix, platform_manifest=None)

Bases: `_PrefixManifestMiddleware`

Pure-ASGI middleware that adds X-Deploy-\* headers on every response.

The header values come from the manifest matching the longest registered
prefix (route_prefix or frontend mount path) for the request path. If no
per-app prefix matches, the platform manifest fills in. Headers are only
added when the underlying value is present — a stub manifest produces a
short header set (just `X-Deploy-App`), not bogus SHAs.

### *class* enlace.manifest.DeployManifest(\*\*data)

Bases: `BaseModel`

What was deployed for one app (or the platform itself).

`app_source` and `platform_source` use the same shape so consumers can
treat them uniformly. `externals` is a free-form map keyed by package
name — the manifest format is intentionally permissive about which keys
appear, since which externals matter varies by app.

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### *class* enlace.manifest.DeployMetaTagMiddleware(app, , manifests_by_prefix, platform_manifest=None)

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

### *class* enlace.manifest.ExternalRef(\*\*data)

Bases: `BaseModel`

Identity for an externally-installed dependency (e.g. an editable sibling).

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### enlace.manifest.MANIFEST_ERROR_KEY *= 'manifest_error'*

`extra` key naming why an on-disk manifest was not used. It exists only
when something is wrong, so a healthy `/_meta` is quiet and a degraded
one cannot be mistaken for “no manifest was ever written”.

### *class* enlace.manifest.SourceRef(\*\*data)

Bases: `BaseModel`

Git identity for a single source tree (app or platform).

#### model_config *: [ClassVar](https://docs.python.org/3/library/typing.html#typing.ClassVar)[ConfigDict]* *= {}*

Configuration for the model, should be a dictionary conforming to [`ConfigDict`][pydantic.config.ConfigDict].

### enlace.manifest.load_manifest(app_name, manifest_dir, , enlace_version=None)

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
  [`DeployManifest`](#enlace.manifest.DeployManifest)

### enlace.manifest.load_platform_manifest(manifest_dir, , enlace_version=None)

Load the platform-level deploy manifest (or a minimal stub).

* **Return type:**
  [`DeployManifest`](#enlace.manifest.DeployManifest)

### enlace.manifest.resolve_manifest_dir(config_manifest_dir=None)

Resolve the manifest directory from env var or config.

`ENLACE_MANIFEST_DIR` takes precedence over the config value. Returns
`None` when neither is set — callers treat that as “no on-disk manifests
available; serve minimal stubs”.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)]
