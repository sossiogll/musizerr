"""A throwaway API for scripts/test-frontend-image.sh: it answers every request with what it received.

  GET /<anything>   200, JSON {"server": NAME, "path": ..., "query": ..., "headers": {...}}
  GET /progress     200, a text/event-stream: one event now, the second after DELAY seconds

Run: python frontend_test_upstream.py NAME [PORT] [DELAY]. Standard library only.
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

NAME = sys.argv[1] if len(sys.argv) > 1 else "upstream"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
DELAY = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path == "/progress":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for index in (1, 2):
                chunk = f"data: {index} {NAME} {self.headers.get('X-Forwarded-Proto')}\n\n".encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                if index == 1:
                    time.sleep(DELAY)
            self.wfile.write(b"0\r\n\r\n")
            return
        body = json.dumps({
            "server": NAME, "path": parts.path, "query": parts.query, "headers": {k.lower(): v for k, v in self.headers.items()},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
