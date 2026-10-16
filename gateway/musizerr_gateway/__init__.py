"""The Musizerr provider gateway.

Host infrastructure: it pulls provider images by digest, unpacks them, runs each
installed provider in its own bubblewrap sandbox and decides what the sandbox may
reach. It imports the standard library and ``musizerr_sdk`` (the protocol, the wire
codec, the manifest types) and nothing of the application: never ``app/``'s
packages, never its database (``tests/test_layering.py``).
"""
