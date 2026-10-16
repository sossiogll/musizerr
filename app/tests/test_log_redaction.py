"""
Nessuna credenziale nei log: un URL con parametri d'autenticazione (token,
salt, utente, API key) o con userinfo è loggato con i valori sostituiti da
``***``, qualunque logger lo emetta — compresa la riga di richiesta di httpx.
"""
from __future__ import annotations

import io
import logging

import httpx
import pytest

from services.errors import public_error
from services.log_redaction import REDACTED, install_log_redaction, redact

SUBSONIC = "http://mediaserver:4533/rest/ping.view?u=alice&t=26719a1196d2a940705a59634eb18eab&s=c19b2d&v=1.16.1&c=musizerr&f=json"


@pytest.fixture
def captured():
    install_log_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def test_subsonic_credentials_are_redacted():
    out = redact(SUBSONIC)

    assert "alice" not in out and "26719a" not in out and "c19b2d" not in out
    assert f"u={REDACTED}&t={REDACTED}&s={REDACTED}" in out
    assert "v=1.16.1&c=musizerr&f=json" in out  # il resto dell'URL resta leggibile


@pytest.mark.parametrize("url, secret", [
    ("https://api.example/v1?api_key=abc123&q=x", "abc123"),
    ("https://api.example/v1?q=x&access_token=tok.en-1", "tok.en-1"),
    ("https://api.example/cb?code=xyz&state=1", "xyz"),
    ("https://alice:hunter2@host.example/path", "hunter2"),
    ("GET https://api.example/v1?Token=UPPER HTTP/1.1", "UPPER"),
])
def test_other_credentials_are_redacted(url, secret):
    assert secret not in redact(url)


def test_text_without_credentials_is_untouched():
    text = "Search budget 5.0s exceeded; dropped: ['a']  q=love&type=track"
    assert redact(text) == text


def test_the_httpx_request_line_is_redacted_in_log_output(captured):
    request = httpx.Request("GET", SUBSONIC)
    # La stessa chiamata che httpx fa per ogni richiesta (livello INFO).
    logging.getLogger("httpx").info(
        'HTTP Request: %s %s "%s %d %s"', request.method, request.url, "HTTP/1.1", 200, "OK",
    )

    out = captured.getvalue()
    assert "HTTP Request: GET http://mediaserver:4533/rest/ping.view?u=***&t=***&s=***" in out
    assert "26719a1196d2a940705a59634eb18eab" not in out and "alice" not in out


def test_a_real_httpx_call_is_redacted_in_log_output(captured):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    with httpx.Client(transport=transport) as client:
        client.get(SUBSONIC)

    out = captured.getvalue()
    assert "HTTP Request" in out
    assert "c19b2d" not in out and "26719a" not in out


def test_exception_text_is_redacted_too(captured):
    try:
        raise RuntimeError(f"Server error '502 Bad Gateway' for url '{SUBSONIC}'")
    except RuntimeError:
        logging.getLogger("workers.tasks").exception("sync failed")

    out = captured.getvalue()
    assert "sync failed" in out and "Traceback" in out
    assert "26719a" not in out and "c19b2d" not in out


def test_the_admin_error_detail_is_redacted():
    body = public_error(f"Server error for url '{SUBSONIC}'", "sync_provider", show_detail=True)
    assert "26719a" not in body["error_detail"] and "t=***" in body["error_detail"]


def test_the_access_log_keeps_its_arguments_and_loses_the_code(captured):
    """uvicorn formatta la riga di accesso spacchettando i suoi 5 argomenti:
    vanno ripuliti uno per uno, non appiattiti."""
    from uvicorn.logging import AccessFormatter

    handler = logging.StreamHandler(captured)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    access = logging.getLogger("uvicorn.access.test")
    access.addHandler(handler)
    access.propagate = False
    try:
        access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET", "/auth/someprovider/callback?code=SECRET&state=1", "1.1", 302)
    finally:
        access.removeHandler(handler)

    out = captured.getvalue()
    assert '"GET /auth/someprovider/callback?code=***&state=1 HTTP/1.1" 302' in out
    assert "SECRET" not in out


def test_numeric_arguments_keep_their_type(captured):
    logging.getLogger("x").info("%d files, %.1f s", 3, 1.5)
    assert "3 files, 1.5 s" in captured.getvalue()
