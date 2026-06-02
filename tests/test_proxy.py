"""Tests for the reverse-proxy ASGI app, focused on streaming timeouts.

The gateway proxies ``/api/*`` to per-app backends. A finite read timeout is
right for ordinary requests (bounds a hung upstream) but wrong for long-lived
streams: an idle Server-Sent-Events connection would be dropped, forcing the
client to reconnect in a loop. ``_request_timeout`` disables only the read
timeout for ``text/event-stream`` requests.
"""

from enlace.proxy import _DEFAULT_TIMEOUT_S, _request_timeout


def test_sse_request_disables_read_timeout():
    """An `Accept: text/event-stream` request gets read=None, others bounded."""
    override = _request_timeout("text/event-stream", _DEFAULT_TIMEOUT_S)
    assert override is not None
    assert override["read"] is None
    # connect / write / pool stay bounded so a stuck handshake still fails.
    assert override["connect"] == _DEFAULT_TIMEOUT_S
    assert override["write"] == _DEFAULT_TIMEOUT_S
    assert override["pool"] == _DEFAULT_TIMEOUT_S


def test_sse_match_is_case_insensitive_and_tolerates_params():
    """EventSource may send a charset/params suffix or odd casing."""
    assert _request_timeout("Text/Event-Stream", 30.0) is not None
    assert _request_timeout("text/event-stream; charset=utf-8", 30.0) is not None


def test_non_streaming_request_uses_client_default():
    """Ordinary requests get no override → the client's bounded timeout."""
    assert _request_timeout("application/json", _DEFAULT_TIMEOUT_S) is None
    assert _request_timeout("", _DEFAULT_TIMEOUT_S) is None
    assert _request_timeout("text/html", _DEFAULT_TIMEOUT_S) is None
