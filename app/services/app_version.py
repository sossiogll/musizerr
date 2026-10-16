"""
The version this instance runs (ADR-0007 §2): written by the build from the git tag
(``MUSIZERR_VERSION``: ``1.4.0`` for a release, ``1.3.2+7.abc1234`` for a build of master) with the
commit (``MUSIZERR_COMMIT``). The development stack sets neither: ``dev``.
"""
from __future__ import annotations

import os

DEVELOPMENT = "dev"

VERSION = os.environ.get("MUSIZERR_VERSION") or DEVELOPMENT
COMMIT = os.environ.get("MUSIZERR_COMMIT") or ""
