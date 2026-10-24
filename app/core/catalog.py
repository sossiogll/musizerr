"""
Catalog version 2: what a source publishes and how the admin's approval is judged.

A source is the URL of a catalog (a JSON file). A catalog lists, for each provider, the OCI
image it is published as, PINNED BY DIGEST, with what the image says of itself and the
permissions its sandbox will be given::

    {"name": "My providers", "version": 2, "providers": [
        {"name": "words",                   # the provider's name (how the host knows it)
         "image": "ghcr.io/owner/musizeer-provider-words",   # registry/repository, no tag
         "digest": "sha256:<64 hex>",
         "provider_api_version": 6,
         "roles": ["lyrics"],
         "label": "Words",
         "description": "Synced lyrics from lyrics.example.org",
         "homepage": "https://lyrics.example.org",              # optional
         "permissions": {"egress": ["lyrics.example.org:443"], "egress_from_settings": [],
                         "filesystem_from_settings": [], "limits": {}}}]}

Everything here is pure: parsing and validating a catalog, saying whether an entry is
compatible with this host, and the difference between two sets of permissions (what the
admin is shown when an update asks for something other than what they approved). Fetching a
catalog and keeping it in step with the database is ``services/sources.py``.

The parse is STRICT. A catalog that is not version 2 (or not a catalog) is refused as a whole,
with the reason; an entry that is malformed is refused alone, with the reason, and the rest
of the catalog is still usable. Unknown fields are refused too: a publisher who needs a new
field needs a new version.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from musizeer_sdk.interfaces import PROVIDER_API_VERSION
from musizeer_sdk.manifest import ROLES
from musizeer_sdk.permissions import (
    FilesystemAccess,
    Permissions,
    PermissionsError,
    TcpAccess,
    canonical,
    from_json,
    to_json,
)

CATALOG_VERSION = 2
MAX_ENTRIES = 500
MAX_LABEL = 100
MAX_DESCRIPTION = 1000
MAX_URL = 500

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$")
_REGISTRY = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:\d{1,5})?$")

_CATALOG_KEYS = {"name", "version", "providers"}
_ENTRY_REQUIRED = {"name", "image", "digest", "provider_api_version", "roles", "label", "description", "permissions"}
_ENTRY_KEYS = _ENTRY_REQUIRED | {"homepage"}
_PERMISSION_KEYS = {"egress", "egress_from_settings", "filesystem_from_settings", "tcp_from_settings", "limits"}
_LIMIT_NAMES = ("memory_mb", "open_files", "processes")


class CatalogError(ValueError):
    """A catalog (or one of its entries) that cannot be used; the message says why."""


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    registry: str
    repository: str
    digest: str
    provider_api_version: int
    roles: tuple[str, ...]
    label: str
    description: str
    permissions: Permissions
    homepage: str | None = None

    @property
    def image(self) -> str:
        return f"{self.registry}/{self.repository}"

    @property
    def reference(self) -> str:
        return f"{self.image}@{self.digest}"

    @property
    def compatible(self) -> bool:
        """It implements the provider contract this host speaks. An entry that does not is listed
        but cannot be installed."""
        return self.provider_api_version == PROVIDER_API_VERSION

    @property
    def incompatibility(self) -> str | None:
        if self.compatible:
            return None
        return (
            f"implements provider API version {self.provider_api_version}, "
            f"this Musizeer needs version {PROVIDER_API_VERSION}"
        )


@dataclass(frozen=True)
class RejectedEntry:
    label: str  # the entry's name when it has one, else its position ("#3")
    reason: str


@dataclass(frozen=True)
class Catalog:
    name: str
    entries: tuple[CatalogEntry, ...]
    rejected: tuple[RejectedEntry, ...] = ()

    def get(self, name: str) -> CatalogEntry | None:
        return next((entry for entry in self.entries if entry.name == name), None)


# ------------------------------------------------------------------
# Parsing
# ------------------------------------------------------------------


def parse_catalog(document: object) -> Catalog:
    """The catalog a JSON document describes; :class:`CatalogError` if it is not a usable
    version 2 catalog. An entry that is not usable is in ``rejected``, not an error."""
    if not isinstance(document, dict):
        raise CatalogError("a catalog is a JSON object")
    if "version" not in document:
        raise CatalogError("not a catalog: it has no 'version' (this Musizeer reads catalog version 2)")
    version = document["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise CatalogError(f"the catalog's 'version' must be the number {CATALOG_VERSION}")
    if version != CATALOG_VERSION:
        hint = " (the pip-package format is no longer supported)" if version == 1 else ""
        raise CatalogError(f"catalog version {version} is not supported{hint}: this Musizeer reads version {CATALOG_VERSION}")
    unknown = sorted(set(document) - _CATALOG_KEYS)
    if unknown:
        raise CatalogError("the catalog has unknown fields: " + ", ".join(map(str, unknown)))
    name = document.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_LABEL:
        raise CatalogError(f"the catalog needs a 'name' (1 to {MAX_LABEL} characters)")
    providers = document.get("providers")
    if not isinstance(providers, list):
        raise CatalogError("the catalog's 'providers' is a list")
    if len(providers) > MAX_ENTRIES:
        raise CatalogError(f"the catalog lists more than {MAX_ENTRIES} providers")

    entries: list[CatalogEntry] = []
    rejected: list[RejectedEntry] = []
    for index, raw in enumerate(providers):
        label = raw["name"] if isinstance(raw, dict) and isinstance(raw.get("name"), str) else f"#{index}"
        try:
            entry = parse_entry(raw)
        except CatalogError as exc:
            rejected.append(RejectedEntry(label, str(exc)))
            continue
        if any(entry.name == other.name for other in entries):
            rejected.append(RejectedEntry(label, f"'{entry.name}' is listed more than once"))
            continue
        entries.append(entry)
    return Catalog(name.strip(), tuple(entries), tuple(rejected))


def parse_entry(raw: object) -> CatalogEntry:
    if not isinstance(raw, dict):
        raise CatalogError("an entry is a JSON object")
    missing = sorted(_ENTRY_REQUIRED - set(raw))
    if missing:
        raise CatalogError("missing fields: " + ", ".join(missing))
    unknown = sorted(set(raw) - _ENTRY_KEYS)
    if unknown:
        raise CatalogError("unknown fields: " + ", ".join(map(str, unknown)))

    name = raw["name"]
    if not isinstance(name, str) or not NAME.match(name):
        raise CatalogError(f"'{name}' is not a provider name (lowercase letters, digits, - and _, starting with a letter)")
    registry, repository = _parse_image(raw["image"])
    digest = raw["digest"]
    if not isinstance(digest, str) or not DIGEST.match(digest):
        raise CatalogError("'digest' must be 'sha256:' followed by 64 lowercase hex digits (images are pinned by digest, never by tag)")
    api_version = raw["provider_api_version"]
    if isinstance(api_version, bool) or not isinstance(api_version, int) or api_version < 1:
        raise CatalogError("'provider_api_version' must be a positive whole number")
    roles = raw["roles"]
    if not isinstance(roles, list) or not roles or not all(isinstance(role, str) for role in roles):
        raise CatalogError("'roles' must be a non-empty list of role names")
    bad = [role for role in roles if role not in ROLES]
    if bad:
        raise CatalogError(f"unknown roles: {', '.join(bad)} (known: {', '.join(sorted(ROLES))})")
    if len(set(roles)) != len(roles):
        raise CatalogError("'roles' lists a role twice")
    label = raw["label"]
    if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL:
        raise CatalogError(f"'label' must be 1 to {MAX_LABEL} characters")
    description = raw["description"]
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
        raise CatalogError(f"'description' must be text of at most {MAX_DESCRIPTION} characters")
    homepage = raw.get("homepage")
    if homepage is not None and not _is_web_url(homepage):
        raise CatalogError("'homepage' must be an http(s) URL")
    return CatalogEntry(
        name=name, registry=registry, repository=repository, digest=digest, provider_api_version=api_version,
        roles=tuple(sorted(roles)), label=label.strip(), description=description.strip(),
        permissions=permissions_from_dict(raw["permissions"]), homepage=homepage,
    )


def _parse_image(image: object) -> tuple[str, str]:
    if not isinstance(image, str):
        raise CatalogError("'image' must be 'registry/repository'")
    if "@" in image:
        raise CatalogError("'image' names no digest: the digest is its own field")
    registry, _, repository = image.partition("/")
    if not repository or not _REGISTRY.match(registry) or ":" in repository:
        raise CatalogError(f"'{image}' is not 'registry/repository' (no tag: the image is pinned by digest)")
    if not _REPOSITORY.match(repository):
        raise CatalogError(f"'{image}' is not a valid repository (lowercase letters, digits, . _ - and /)")
    return registry, repository


def _is_web_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > MAX_URL:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


# ------------------------------------------------------------------
# Permissions as JSON (the form of the image labels)
# ------------------------------------------------------------------


def permissions_from_dict(raw: object) -> Permissions:
    """The (canonical) permissions a JSON object describes; :class:`CatalogError` otherwise."""
    if not isinstance(raw, dict):
        raise CatalogError("'permissions' is an object")
    unknown = sorted(set(raw) - _PERMISSION_KEYS)
    if unknown:
        raise CatalogError("'permissions' has unknown fields: " + ", ".join(map(str, unknown)))
    for key in ("egress", "egress_from_settings", "filesystem_from_settings", "tcp_from_settings"):
        if key in raw and not isinstance(raw[key], list):
            raise CatalogError(f"'{key}' is a list")
    for item in raw.get("filesystem_from_settings") or ():
        if not isinstance(item, dict) or set(item) - {"setting", "mode"} or "setting" not in item:
            raise CatalogError("'filesystem_from_settings' lists objects with a 'setting' and a 'mode' (ro or rw)")
    for item in raw.get("tcp_from_settings") or ():
        if not isinstance(item, dict) or set(item) - {"setting", "ports"} or "setting" not in item:
            raise CatalogError("'tcp_from_settings' lists objects with a 'setting' and 'ports' ('any', a port or a range)")
    limits = raw.get("limits")
    if limits is not None:
        if not isinstance(limits, dict) or set(limits) - set(_LIMIT_NAMES):
            raise CatalogError("'limits' is an object of memory_mb, open_files and processes")
    try:
        return canonical(from_json(json.dumps(raw)))
    except (PermissionsError, TypeError, ValueError) as exc:
        raise CatalogError(str(exc)) from exc


def permissions_to_dict(permissions: Permissions) -> dict[str, Any]:
    """The JSON form of ``permissions`` (canonical, so equal permissions give equal dicts)."""
    return json.loads(to_json(canonical(permissions)))


# ------------------------------------------------------------------
# The difference between two sets of permissions
# ------------------------------------------------------------------


@dataclass(frozen=True)
class LimitChange:
    name: str
    old: int | None  # None: the gateway's default
    new: int | None


@dataclass(frozen=True)
class PermissionsDiff:
    """What an update asks for that the admin did not approve (``added``), and what it no longer
    asks for (``removed``). A filesystem access whose mode changes, and a raw-TCP access whose ports change,
    is a removal and an addition."""

    egress_added: tuple[str, ...] = ()
    egress_removed: tuple[str, ...] = ()
    egress_from_settings_added: tuple[str, ...] = ()
    egress_from_settings_removed: tuple[str, ...] = ()
    filesystem_added: tuple[FilesystemAccess, ...] = ()
    filesystem_removed: tuple[FilesystemAccess, ...] = ()
    tcp_added: tuple[TcpAccess, ...] = ()
    tcp_removed: tuple[TcpAccess, ...] = ()
    limits_changed: tuple[LimitChange, ...] = ()
    roles_added: tuple[str, ...] = ()
    roles_removed: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self == PermissionsDiff()

    @property
    def grants_more(self) -> bool:
        """It asks for something new: another host (a wildcard included), setting-named host, raw-TCP host, path or role, or a
        larger limit (a limit that goes from a number to the default is judged by the gateway's
        ceilings, so it counts as more too)."""
        return bool(
            self.egress_added or self.egress_from_settings_added or self.filesystem_added or self.tcp_added or self.roles_added
            or any(change.new is None or (change.old is not None and change.new > change.old) for change in self.limits_changed)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "egress": {"added": list(self.egress_added), "removed": list(self.egress_removed)},
            "egress_from_settings": {
                "added": list(self.egress_from_settings_added), "removed": list(self.egress_from_settings_removed),
            },
            "filesystem_from_settings": {
                "added": [{"setting": a.setting, "mode": a.mode} for a in self.filesystem_added],
                "removed": [{"setting": a.setting, "mode": a.mode} for a in self.filesystem_removed],
            },
            "tcp_from_settings": {
                "added": [{"setting": a.setting, "ports": a.ports} for a in self.tcp_added],
                "removed": [{"setting": a.setting, "ports": a.ports} for a in self.tcp_removed],
            },
            "limits": [{"name": c.name, "old": c.old, "new": c.new} for c in self.limits_changed],
            "roles": {"added": list(self.roles_added), "removed": list(self.roles_removed)},
            "is_empty": self.is_empty,
            "grants_more": self.grants_more,
        }


def diff_permissions(
    old: Permissions, new: Permissions, old_roles: tuple[str, ...] = (), new_roles: tuple[str, ...] = (),
) -> PermissionsDiff:
    """What changes from what was approved (``old``) to what is offered (``new``)."""
    old, new = canonical(old), canonical(new)

    def added(before, after):
        return tuple(item for item in after if item not in before)

    return PermissionsDiff(
        egress_added=added(old.egress, new.egress),
        egress_removed=added(new.egress, old.egress),
        egress_from_settings_added=added(old.egress_from_settings, new.egress_from_settings),
        egress_from_settings_removed=added(new.egress_from_settings, old.egress_from_settings),
        filesystem_added=added(old.filesystem_from_settings, new.filesystem_from_settings),
        filesystem_removed=added(new.filesystem_from_settings, old.filesystem_from_settings),
        tcp_added=added(old.tcp_from_settings, new.tcp_from_settings),
        tcp_removed=added(new.tcp_from_settings, old.tcp_from_settings),
        limits_changed=tuple(
            LimitChange(name, getattr(old.limits, name), getattr(new.limits, name))
            for name in _LIMIT_NAMES if getattr(old.limits, name) != getattr(new.limits, name)
        ),
        roles_added=tuple(sorted(set(new_roles) - set(old_roles))),
        roles_removed=tuple(sorted(set(old_roles) - set(new_roles))),
    )
