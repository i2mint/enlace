"""Compression that knows what it must not compress.

Starlette's ``GZipMiddleware`` compresses *every* response above ``minimum_size`` when the
client offers gzip. On a platform that also serves media, that is wrong in two ways, and
enlace hit both.

**1. It corrupts byte-range responses.** ``GZipMiddleware`` has no exclusion for ``206 Partial
Content`` or ``Content-Range``. Wrapped around a ``StaticFiles`` mount serving video, it gzips
the partial body while ``Content-Range`` still describes the *uncompressed* representation.
Observed against a real mp4 through enlace::

    HTTP/2 206
    content-encoding: gzip
    content-range:   bytes 0-65535/112651   <- offsets into the UNCOMPRESSED file
    content-length:  64076                  <- length of the COMPRESSED body

Per RFC 9110 §14.4 the range describes the selected representation, so those headers now
disagree. Byte ranges are the entire basis of ``<video>`` playback — seeking, streaming, and
Safari's refusal to play media at all without them — so this quietly undermines the very thing
a static media mount exists to provide.

**2. It burns the shared event loop for nothing.** Video, audio, most images and archives are
already compressed: gzipping them buys ~1% for the full CPU cost, and Starlette compresses
*synchronously inside the asyncio loop* — a loop that, in enlace, is shared by every app on
the platform.

So: compress text, never compress a ranged exchange, never compress bytes that are already
compressed. Pure ASGI (enlace forbids ``BaseHTTPMiddleware``).
"""

from __future__ import annotations

import gzip
import io

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Content types whose bytes are already compressed. ``image/svg+xml`` is deliberately NOT
#: here — SVG is text and compresses extremely well.
INCOMPRESSIBLE_PREFIXES: tuple[str, ...] = ("video/", "audio/", "image/", "font/woff")

INCOMPRESSIBLE_TYPES: frozenset[str] = frozenset({
    "application/zip",
    "application/gzip",
    "application/x-gzip",
    "application/zstd",
    "application/pdf",
    "application/wasm",
})

COMPRESSIBLE_EXCEPTIONS: frozenset[str] = frozenset({"image/svg+xml"})


def is_compressible(content_type: str) -> bool:
    """Whether a response of this content type is worth compressing."""
    ctype = content_type.split(";", 1)[0].strip().lower()
    if not ctype:
        return True  # unknown — let the size threshold decide
    if ctype in COMPRESSIBLE_EXCEPTIONS:
        return True
    if ctype in INCOMPRESSIBLE_TYPES:
        return False
    return not ctype.startswith(INCOMPRESSIBLE_PREFIXES)


class SelectiveGZipMiddleware:
    """Gzip, minus the responses that must not be compressed.

    Drop-in replacement for ``starlette.middleware.gzip.GZipMiddleware``.
    """

    def __init__(self, app: ASGIApp, minimum_size: int = 1024, compresslevel: int = 9):
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_headers = Headers(scope=scope)
        if "gzip" not in request_headers.get("accept-encoding", ""):
            await self.app(scope, receive, send)
            return

        # A ranged request can never be safely compressed here: the body we would encode is a
        # slice, but Content-Range describes the whole representation. This is a property of
        # the REQUEST, so we can settle it before the response exists — and it is the case
        # that matters, because every `<video>` fetch is a ranged fetch.
        if "range" in request_headers:
            await self.app(scope, receive, send)
            return

        await _Responder(self.app, self.minimum_size, self.compresslevel)(scope, receive, send)


class _Responder:
    """Compresses a response, deciding from its ``http.response.start`` message.

    The body is streamed, never buffered whole; only the start message is held, just long
    enough to read status and ``Content-Type``.
    """

    def __init__(self, app: ASGIApp, minimum_size: int, compresslevel: int):
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel
        self.send: Send = _unattached_send
        self.initial_message: Message = {}
        self.started = False
        self.passthrough = False
        self.buffer = io.BytesIO()
        self.gzip_file: gzip.GzipFile | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.send = send
        await self.app(scope, receive, self._send)

    def _should_compress(self) -> bool:
        headers = Headers(raw=self.initial_message["headers"])
        status = self.initial_message["status"]
        if status == 206 or "content-range" in headers:
            return False  # a ranged response: see the class docstring
        if "content-encoding" in headers:
            return False  # already encoded — never double-encode
        return is_compressible(headers.get("content-type", ""))

    async def _send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.initial_message = message  # hold it: we need the body to decide
            return

        if message["type"] != "http.response.body":
            await self.send(message)
            return

        body: bytes = message.get("body", b"")
        more_body: bool = message.get("more_body", False)

        if not self.started:
            self.started = True
            small_and_complete = not more_body and len(body) < self.minimum_size
            if not self._should_compress() or small_and_complete:
                self.passthrough = True
                await self.send(self.initial_message)
                await self.send(message)
                return

            self.gzip_file = gzip.GzipFile(
                mode="wb", fileobj=self.buffer, compresslevel=self.compresslevel
            )
            headers = MutableHeaders(raw=self.initial_message["headers"])
            headers["Content-Encoding"] = "gzip"
            headers.add_vary_header("Accept-Encoding")
            if not more_body:
                # Whole body in one message: we know the compressed length exactly.
                self.gzip_file.write(body)
                self.gzip_file.close()
                compressed = self.buffer.getvalue()
                headers["Content-Length"] = str(len(compressed))
                message["body"] = compressed
                await self.send(self.initial_message)
                await self.send(message)
                return
            # Streaming: the final length is unknown, so it must not be advertised.
            del headers["Content-Length"]
            await self.send(self.initial_message)

        if self.passthrough:
            await self.send(message)
            return

        assert self.gzip_file is not None
        self.gzip_file.write(body)
        if not more_body:
            self.gzip_file.close()
        message["body"] = self.buffer.getvalue()
        self.buffer.seek(0)
        self.buffer.truncate()
        await self.send(message)


async def _unattached_send(message: Message) -> None:  # pragma: no cover
    raise RuntimeError("send awaitable not set")
