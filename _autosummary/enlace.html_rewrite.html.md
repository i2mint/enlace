# enlace.html_rewrite

Rewrite HTML response bodies from pure-ASGI middleware.

Several platform features edit the `<head>` of the HTML an app serves (deploy
`<meta>` tags, the app’s icon links). Each needs the same careful dance —
hold `http.response.start` until the whole body is buffered, rewrite it, fix
`Content-Length` — and only for `text/html`. That dance lives here once.

A middleware that rewrites bodies must sit **inside** any compression
middleware, so it sees and edits uncompressed bytes.

### Functions

| [`header_value`](#enlace.html_rewrite.header_value)(headers, name)                     | Return the first matching header value (case-insensitive), or None.   |
|--------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------|
| [`inject_into_head`](#enlace.html_rewrite.inject_into_head)(body, snippet)                 | Insert `snippet` into `body` just before `</head>`.                   |
| [`rewrite_html_response`](#enlace.html_rewrite.rewrite_html_response)(app, scope, receive, ...) | Run `app`, passing any `text/html` response body through `rewrite`.   |

### enlace.html_rewrite.header_value(headers, name)

Return the first matching header value (case-insensitive), or None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes)]

### enlace.html_rewrite.inject_into_head(body, snippet)

Insert `snippet` into `body` just before `</head>`.

Falls back to just after an opening `<head ...>` tag, then to
prepending — so even malformed HTML still carries the tags.

* **Return type:**
  [`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes)

### *async* enlace.html_rewrite.rewrite_html_response(app, scope, receive, send, rewrite)

Run `app`, passing any `text/html` response body through `rewrite`.

Non-HTML responses stream through untouched. For HTML, the start message is
held until the full body is buffered (the rewrite changes its length), then
sent with a corrected `Content-Length`.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)
