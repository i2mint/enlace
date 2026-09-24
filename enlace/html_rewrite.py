"""Rewrite HTML response bodies from pure-ASGI middleware.

Several platform features edit the ``<head>`` of the HTML an app serves (deploy
``<meta>`` tags, the app's icon links). Each needs the same careful dance —
hold ``http.response.start`` until the whole body is buffered, rewrite it, fix
``Content-Length`` — and only for ``text/html``. That dance lives here once.

A middleware that rewrites bodies must sit **inside** any compression
middleware, so it sees and edits uncompressed bytes.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

HEAD_CLOSE_RE = re.compile(rb"</head\s*>", re.IGNORECASE)
HEAD_OPEN_RE = re.compile(rb"<head[^>]*>", re.IGNORECASE)


def inject_into_head(body: bytes, snippet: bytes) -> bytes:
    """Insert ``snippet`` into ``body`` just before ``</head>``.

    Falls back to just after an opening ``<head ...>`` tag, then to
    prepending — so even malformed HTML still carries the tags.
    """
    m = HEAD_CLOSE_RE.search(body)
    if m:
        return body[: m.start()] + snippet + body[m.start() :]
    m = HEAD_OPEN_RE.search(body)
    if m:
        return body[: m.end()] + snippet + body[m.end() :]
    return snippet + body


def header_value(headers, name: bytes) -> Optional[bytes]:
    """Return the first matching header value (case-insensitive), or None."""
    lname = name.lower()
    for k, v in headers:
        if k.lower() == lname:
            return v
    return None


async def rewrite_html_response(
    app,
    scope,
    receive,
    send,
    rewrite: Callable[[bytes], bytes],
) -> None:
    """Run ``app``, passing any ``text/html`` response body through ``rewrite``.

    Non-HTML responses stream through untouched. For HTML, the start message is
    held until the full body is buffered (the rewrite changes its length), then
    sent with a corrected ``Content-Length``.
    """
    start_message: Optional[dict] = None
    chunks: list[bytes] = []
    intercepting = False

    async def send_wrapper(message):
        nonlocal start_message, intercepting
        mtype = message["type"]

        if mtype == "http.response.start":
            content_type = header_value(message.get("headers", []), b"content-type")
            if content_type and content_type.lower().startswith(b"text/html"):
                intercepting = True
                start_message = message
                return  # held until body is complete
            await send(message)
            return

        if mtype == "http.response.body" and intercepting:
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return  # keep buffering until the last chunk
            new_body = rewrite(b"".join(chunks))
            assert start_message is not None
            headers = [
                (k, v)
                for (k, v) in start_message.get("headers", [])
                if k.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(new_body)).encode()))
            await send({**start_message, "headers": headers})
            await send(
                {"type": "http.response.body", "body": new_body, "more_body": False}
            )
            return

        await send(message)

    await app(scope, receive, send_wrapper)
