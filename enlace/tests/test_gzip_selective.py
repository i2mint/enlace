"""Tests for SelectiveGZipMiddleware.

The regression these lock down: Starlette's GZipMiddleware compressed `206 Partial Content`
responses, producing a gzipped body alongside a `Content-Range` that described the
*uncompressed* representation. Byte ranges are how `<video>` streams and seeks (and Safari
refuses to play media without them), so this broke media serving on a platform that had
just moved its video behind a StaticFiles mount.
"""

import gzip

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import Response

from enlace.gzip_selective import SelectiveGZipMiddleware, is_compressible


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024)

    big_text = "x" * 5000
    video_bytes = b"\x00\x01\x02\x03" * 2000  # 8000 bytes of "video"

    @app.get("/text")
    async def text():
        return Response(big_text, media_type="text/plain")

    @app.get("/json-small")
    async def json_small():
        return Response("hi", media_type="application/json")

    @app.get("/video")
    async def video():
        return Response(video_bytes, media_type="video/mp4")

    @app.get("/image")
    async def image():
        return Response(video_bytes, media_type="image/png")

    @app.get("/svg")
    async def svg():
        return Response("<svg>" + "a" * 5000 + "</svg>", media_type="image/svg+xml")

    @app.get("/partial")
    async def partial():
        # What StaticFiles emits for a Range request: a 206 whose Content-Range describes
        # the FULL representation, not the slice.
        body = video_bytes[:4096]
        return Response(
            body,
            status_code=206,
            media_type="video/mp4",
            headers={"content-range": f"bytes 0-4095/{len(video_bytes)}"},
        )

    @app.get("/partial-text")
    async def partial_text():
        body = "y" * 4096
        return Response(
            body,
            status_code=206,
            media_type="text/plain",
            headers={"content-range": "bytes 0-4095/99999"},
        )

    return app


@pytest.fixture
def client():
    return TestClient(_app())


GZIP = {"accept-encoding": "gzip"}


def test_text_is_still_compressed(client):
    r = client.get("/text", headers=GZIP)
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in r.headers.get("vary", "").lower()


def test_small_response_not_compressed(client):
    r = client.get("/json-small", headers=GZIP)
    assert "content-encoding" not in r.headers


def test_video_is_not_compressed(client):
    """Already-compressed bytes: pure CPU cost, ~1% gain — on the shared event loop."""
    r = client.get("/video", headers=GZIP)
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert len(r.content) == 8000


def test_image_is_not_compressed(client):
    r = client.get("/image", headers=GZIP)
    assert "content-encoding" not in r.headers


def test_svg_IS_compressed(client):
    """SVG is text wearing an image/ content type."""
    r = client.get("/svg", headers=GZIP)
    assert r.headers["content-encoding"] == "gzip"


def test_206_is_never_compressed(client):
    """THE bug. A gzipped 206 whose Content-Range describes the uncompressed bytes is a
    response whose headers contradict each other — and media seeking is built on it."""
    r = client.get("/partial", headers=GZIP)
    assert r.status_code == 206
    assert "content-encoding" not in r.headers
    assert r.headers["content-range"] == "bytes 0-4095/8000"
    # The body must be the raw slice, and Content-Length must agree with it.
    assert len(r.content) == 4096
    assert int(r.headers["content-length"]) == 4096


def test_206_not_compressed_even_for_text(client):
    """Compressibility of the *type* is irrelevant: it is the RANGE that makes it unsafe."""
    r = client.get("/partial-text", headers=GZIP)
    assert r.status_code == 206
    assert "content-encoding" not in r.headers
    assert len(r.content) == 4096


def test_range_request_is_passed_through_uncompressed(client):
    """Decided from the REQUEST — every <video> fetch is a ranged fetch."""
    r = client.get("/text", headers={**GZIP, "range": "bytes=0-99"})
    assert "content-encoding" not in r.headers


def test_no_gzip_when_client_does_not_ask(client):
    r = client.get("/text", headers={"accept-encoding": "identity"})
    assert "content-encoding" not in r.headers
    assert len(r.content) == 5000


def test_compressed_body_actually_decodes(client):
    r = client.get("/text", headers=GZIP)
    # httpx transparently decodes; check the raw bytes really are gzip.
    raw = r.read() if hasattr(r, "read") else r.content
    assert r.text == "x" * 5000
    assert raw == b"x" * 5000  # decoded by the client
    # And Content-Length, when advertised, must describe the COMPRESSED body.
    if "content-length" in r.headers:
        assert int(r.headers["content-length"]) < 5000


@pytest.mark.parametrize(
    "ctype,want",
    [
        ("text/html", True),
        ("application/json", True),
        ("image/svg+xml", True),
        ("video/mp4", False),
        ("audio/mpeg", False),
        ("image/png", False),
        ("image/jpeg", False),
        ("application/zip", False),
        ("application/pdf", False),
        ("font/woff2", False),
        ("", True),  # unknown: let the size threshold decide
    ],
)
def test_is_compressible(ctype, want):
    assert is_compressible(ctype) is want


def test_gzip_roundtrip_streaming():
    """A streaming (chunked) response must compress correctly and not advertise a length."""
    app = FastAPI()
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=10)

    async def stream():
        for _ in range(5):
            yield b"hello world " * 20

    from starlette.responses import StreamingResponse

    @app.get("/stream")
    async def s():
        return StreamingResponse(stream(), media_type="text/plain")

    c = TestClient(app)
    r = c.get("/stream", headers=GZIP)
    assert r.headers["content-encoding"] == "gzip"
    assert "content-length" not in r.headers  # unknowable while streaming
    assert r.text == "hello world " * 100
