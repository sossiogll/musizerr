"""
The versions of Musizerr and what an upgrade from one to another implies (ADR-0007).

Pure rules, no I/O: ``services/update_check.py`` gathers the facts (the version this instance runs, the
releases published and their ``release.json``, the providers installed) and asks here.

A version is ``MAJOR.MINOR.PATCH`` (the git tag ``vX.Y.Z`` without the ``v``). A build of master after a
release carries build metadata, ``1.3.2+7.abc1234`` (seven commits after v1.3.2): it ranks as the release
it is based on, so the next release counts as newer. The development stack is ``dev``: no version at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z.-]+)?$")

# The reasons an update needs more than pulling the new images (the codes the API returns).
REASON_MAJOR = "major"
REASON_UPGRADE_PATH = "upgrade_path"
REASON_CONTRACT_UNSUPPORTED = "contract_unsupported"
REASON_CONTRACT_MIGRATIONS = "contract_migrations"
REASON_MANUAL_STEPS = "manual_steps"
REASON_NO_METADATA = "no_metadata"

KIND_MAJOR, KIND_MINOR, KIND_PATCH = "major", "minor", "patch"


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def parse_version(text: str | None) -> Version | None:
    """The version a tag or a build names; None for anything else (``dev``, a prerelease, garbage)."""
    match = _VERSION.match((text or "").strip())
    return Version(*(int(part) for part in match.groups())) if match else None


def update_kind(current: Version, target: Version) -> str:
    if target.major != current.major:
        return KIND_MAJOR
    return KIND_MINOR if target.minor != current.minor else KIND_PATCH


@dataclass(frozen=True)
class InstalledContract:
    """An installed provider and the contract version it implements."""

    name: str
    provider_api_version: int


def assess_update(current: Version, target: Version, meta: dict[str, Any] | None,
                  installed: Iterable[InstalledContract]) -> list[dict[str, Any]]:
    """Why updating from ``current`` to ``target`` needs care: an empty list when pulling the new images
    is all it takes. ``meta`` is the release's ``release.json`` (None when the release has none: its
    contracts and migrations cannot be checked)."""
    reasons: list[dict[str, Any]] = []
    if target.major != current.major:
        reasons.append({"code": REASON_MAJOR})
    if meta is None:
        reasons.append({"code": REASON_NO_METADATA})
        return reasons
    oldest = parse_version(meta.get("min_upgrade_from"))
    if oldest is not None and current < oldest:
        reasons.append({"code": REASON_UPGRADE_PATH, "through": str(oldest)})
    contracts = meta.get("provider_contracts")
    if isinstance(contracts, list):
        unsupported = sorted(p.name for p in installed if p.provider_api_version not in contracts)
        if unsupported:
            reasons.append({"code": REASON_CONTRACT_UNSUPPORTED, "providers": unsupported})
    if meta.get("contract_migrations"):
        reasons.append({"code": REASON_CONTRACT_MIGRATIONS})
    if meta.get("manual_steps"):
        reasons.append({"code": REASON_MANUAL_STEPS})
    return reasons
