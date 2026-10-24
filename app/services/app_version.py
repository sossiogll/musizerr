"""
The version this instance runs (ADR-0007 §2): written by the build from the git tag
(``MUSIZEER_VERSION``: ``1.4.0`` for a release, ``1.3.2+7.abc1234`` for a build of master) with the
commit (``MUSIZEER_COMMIT``). The development stack sets neither: ``dev``.
"""
from __future__ import annotations

from core import environment

VERSION = environment.get("MUSIZEER_VERSION")
COMMIT = environment.get("MUSIZEER_COMMIT")
DEVELOPMENT = environment.default("MUSIZEER_VERSION")
