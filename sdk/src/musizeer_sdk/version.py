"""
The version of this SDK, and how the two sides of a call tell theirs (ADR-0007 §3.1).

Within a contract major, the SDK only adds: optional methods, declarations, fields with a default. A field
added after the first release of the major says since which minor it exists (``wire.SINCE``), and each side
of a call encodes for the other one's minor, leaving out what it would not know: the host learns a provider's
from its manifest (``Manifest.sdk_version``), a runner the host's from each request (``"sdk"``). A peer that
does not say its version was built before this rule existed: ``BASELINE``.
"""
from __future__ import annotations

import re

SDK_VERSION = "5.5.0"

# The minor of a peer that does not say its version: every SDK before 5.5.
BASELINE: tuple[int, int] = (5, 4)

_MINOR = re.compile(r"^(\d+)\.(\d+)(?:\.\d+)?")


def minor_of(version: object) -> tuple[int, int] | None:
    """(major, minor) of ``"5.5.0"`` or ``"5.5"``; None for anything else."""
    match = _MINOR.match(version) if isinstance(version, str) else None
    return (int(match.group(1)), int(match.group(2))) if match else None


def peer_minor(version: object) -> tuple[int, int]:
    """The minor to encode for a peer that said ``version`` (BASELINE when it said nothing usable)."""
    return minor_of(version) or BASELINE
