"""``python -m musizerr_gateway.ctl [list|health|install|grant]``: ask the running gateway over its control socket.

Prints the JSON reply and exits with 0 for ``ok``, 1 for an error reply, 2 if the gateway does not answer.
For an operator (``docker compose exec provider-gateway python -m musizerr_gateway.ctl health``) and the
integration tests. ``install`` reads ``{"providers": [...], "generation": "..."}`` from standard input
and pushes it as the desired set, exactly as the host does (the database of the application is what
normally decides: this is for tests and for debugging a gateway on its own). ``grant`` reads
``{"provider": ..., "targets": [...], "tcp": {...}, "paths": {...}, "ttl": 120}`` and sends it as the
host's grant (the destinations, raw-TCP hosts and folders the provider's settings name). The reply never
contains a credential, and neither does anything this prints.
"""
from __future__ import annotations

import json
import socket
import sys

from .config import from_environment

OPERATIONS = ("list", "health", "install", "grant")


def main(argv: list[str] | None = None, stdin=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    operation = argv[0] if argv else "health"
    if operation not in OPERATIONS:
        print(f"usage: python -m musizerr_gateway.ctl [{'|'.join(OPERATIONS)}]", file=sys.stderr)
        return 2
    request: dict = {"op": "egress" if operation == "grant" else operation}
    if operation in ("install", "grant"):
        try:
            body = json.loads((stdin or sys.stdin).read())
        except ValueError as exc:
            print(f"standard input is not JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(body, dict):
            print("standard input is a JSON object", file=sys.stderr)
            return 2
        if operation == "install":
            request.update(providers=body.get("providers"), generation=body.get("generation"))
        else:
            request.update(
                provider=body.get("provider"), targets=body.get("targets", []), ttl=body.get("ttl", 120),
                tcp=body.get("tcp"), paths=body.get("paths"),
            )
    path = from_environment().control_socket
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(30)
            connection.connect(str(path))
            connection.sendall(json.dumps(request).encode() + b"\n")
            reply = connection.makefile("rb").readline()
        document = json.loads(reply)
    except (OSError, ValueError) as exc:
        print(f"the gateway at {path} does not answer: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0 if document.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
