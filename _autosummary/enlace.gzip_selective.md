# enlace.gzip_selective

Compression that knows what it must not compress.

Starlette’s `GZipMiddleware` compresses *every* response above `minimum_size`
when the client offers gzip. On a platform that also serves media, that is wrong in
two ways, and enlace hit both.

**1. It corrupts byte-range responses.** `GZipMiddleware` has no exclusion for
`206 Partial Content` or `Content-Range`. Wrapped around a `StaticFiles` mount
serving video, it gzips the partial body while `Content-Range` still describes the
*uncompressed* representation. Observed against a real mp4 through enlace:

```default
HTTP/2 206
content-encoding: gzip
content-range:   bytes 0-65535/112651   <- offsets into the UNCOMPRESSED file
content-length:  64076                  <- length of the COMPRESSED body
```

Per RFC 9110 §14.4 the range describes the selected representation, so those headers
now disagree. Byte ranges are the entire basis of `<video>` playback — seeking,
streaming, and Safari’s refusal to play media at all without them — so this quietly
undermines the very thing a static media mount exists to provide.

**2. It burns the shared event loop for nothing.** Video, audio, most images and
archives are already compressed: gzipping them buys ~1% for the full CPU cost, and
Starlette compresses *synchronously inside the asyncio loop* — a loop that, in
enlace, is shared by every app on the platform.

So: compress text, never compress a ranged exchange, never compress bytes that are
already compressed. Pure ASGI (enlace forbids `BaseHTTPMiddleware`).

### Module Attributes

| [`INCOMPRESSIBLE_PREFIXES`](#enlace.gzip_selective.INCOMPRESSIBLE_PREFIXES)   | Content types whose bytes are already compressed.   |
|----------------------------------------------------------------------------|-----------------------------------------------------|

### Functions

| [`is_compressible`](#enlace.gzip_selective.is_compressible)(content_type)   | Whether a response of this content type is worth compressing.   |
|----------------------------------------------------------------------------------|-----------------------------------------------------------------|

### Classes

| [`SelectiveGZipMiddleware`](#enlace.gzip_selective.SelectiveGZipMiddleware)(app[, minimum_size, ...])   | Gzip, minus the responses that must not be compressed.   |
|------------------------------------------------------------------------------------------------------|----------------------------------------------------------|

### enlace.gzip_selective.INCOMPRESSIBLE_PREFIXES *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), ...]* *= ('video/', 'audio/', 'image/', 'font/woff')*

Content types whose bytes are already compressed. `image/svg+xml` is deliberately
NOT here — SVG is text and compresses extremely well.

### *class* enlace.gzip_selective.SelectiveGZipMiddleware(app, minimum_size=1024, compresslevel=9)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Gzip, minus the responses that must not be compressed.

Drop-in replacement for `starlette.middleware.gzip.GZipMiddleware`.

### enlace.gzip_selective.is_compressible(content_type)

Whether a response of this content type is worth compressing.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)
