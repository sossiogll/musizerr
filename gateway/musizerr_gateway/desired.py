"""
Which providers the host wants: the desired set it pushes over the control socket.

The database of the application is the only source of truth for what is installed;
the gateway keeps no list of its own. The host tells it (``{"op": "install", ...}``,
see ``control.py``) when it starts, after every install, update or removal, and
whenever the gateway restarted, and the gateway makes the running set match. One
entry::

    {"name": "lrclib",
     "image": "ghcr.io/sossiogll/musizerr-provider-lrclib",
     "digest": "sha256:<64 hex>",
     "roles": ["lyrics"],
     "permissions": {"egress": ["lrclib.net:443"], "egress_from_settings": [],
                     "filesystem_from_settings": [], "limits": {}},
     "insecure": false,
     "credential": "<token>"}

``image`` is a registry and a repository (a ``@sha256:`` suffix is accepted and must
agree with ``digest``). The image is ALWAYS pulled by ``digest``, never by tag.
``roles`` and ``permissions`` are what the admin APPROVED: the image is refused when its
labels declare anything else (an image cannot gain a permission nobody approved).
``insecure`` marks a registry that may be spoken to over plain HTTP (a local registry of
images under test); only an entry that says so gets it, and such an entry never carries a
credential. ``credential`` (optional) is used to pull this image and only that: it is
held in memory while the image is pulled and is never written to disk, logged or
reported back.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from musizerr_sdk.manifest import ROLES
from musizerr_sdk.permissions import Permissions, PermissionsError, canonical, from_json

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$")
_REGISTRY = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:\d{1,5})?$")
MAX_PROVIDERS = 256
MAX_CREDENTIAL = 4096


class DesiredError(ValueError):
    """An entry of the desired set that cannot be used."""


@dataclass(frozen=True)
class DesiredProvider:
    """A provider the host wants running: its image, pinned by digest, and what was approved for it."""

    name: str
    registry: str
    repository: str
    digest: str
    roles: tuple[str, ...]
    permissions: Permissions
    insecure: bool = False

    @property
    def image(self) -> str:
        return f"{self.registry}/{self.repository}"

    @property
    def reference(self) -> str:
        return f"{self.image}@{self.digest}"


def parse_entry(raw: object) -> tuple[DesiredProvider, str | None]:
    """The entry and its credential (which is not part of the entry: it is not compared, not
    reported and not kept once the image is pulled)."""
    if not isinstance(raw, dict):
        raise DesiredError("an entry is a JSON object")
    name, image, digest = raw.get("name"), raw.get("image"), raw.get("digest")
    if not isinstance(name, str) or not NAME.match(name):
        raise DesiredError(f"'{name}' is not a provider name")
    if not isinstance(image, str):
        raise DesiredError(f"{name}: no image")
    if "@" in image:
        image, _, pinned = image.partition("@")
        if digest is not None and digest != pinned:
            raise DesiredError(f"{name}: the image and the digest name different digests")
        digest = pinned
    if not isinstance(digest, str) or not DIGEST.match(digest):
        raise DesiredError(f"{name}: a 'sha256:<64 hex>' digest is required (images are pulled by digest, never by tag)")
    registry, _, repository = image.partition("/")
    if not repository or not _REGISTRY.match(registry) or ":" in repository or not _REPOSITORY.match(repository):
        raise DesiredError(f"{name}: '{image}' is not 'registry/repository'")
    insecure = raw.get("insecure", False)
    if not isinstance(insecure, bool):
        raise DesiredError(f"{name}: 'insecure' is true or false")
    roles = raw.get("roles")
    if (
        not isinstance(roles, list) or not roles
        or not all(isinstance(role, str) and role in ROLES for role in roles)
    ):
        raise DesiredError(f"{name}: 'roles' is a non-empty list of roles ({', '.join(sorted(ROLES))})")
    permissions = raw.get("permissions")
    if not isinstance(permissions, dict):
        raise DesiredError(f"{name}: the approved 'permissions' are required")
    try:
        approved = canonical(from_json(json.dumps(permissions)))
    except PermissionsError as exc:
        raise DesiredError(f"{name}: {exc}") from exc
    credential = raw.get("credential")
    if credential is not None:
        if (
            not isinstance(credential, str) or not credential or len(credential) > MAX_CREDENTIAL
            or any(ord(char) < 32 or ord(char) == 127 for char in credential)
        ):
            raise DesiredError(f"{name}: the credential is a printable string")
        if insecure:
            raise DesiredError(f"{name}: a credential is never sent over plain HTTP, and 'insecure' allows only that")
    return (
        DesiredProvider(name, registry, repository, digest, tuple(sorted(set(roles))), approved, insecure),
        credential,
    )


def parse_set(raw: object) -> tuple[list[DesiredProvider], dict[str, str], dict[str, str]]:
    """``(usable entries, credentials by name, why each other entry is not usable)``.

    Raises :class:`DesiredError` for a set that is not a list at all; one bad entry refuses
    that entry only."""
    if not isinstance(raw, list):
        raise DesiredError("'providers' is a list of entries")
    if len(raw) > MAX_PROVIDERS:
        raise DesiredError(f"more than {MAX_PROVIDERS} providers")
    usable: list[DesiredProvider] = []
    credentials: dict[str, str] = {}
    refused: dict[str, str] = {}
    for index, item in enumerate(raw):
        label = item.get("name") if isinstance(item, dict) and isinstance(item.get("name"), str) else f"#{index}"
        try:
            entry, credential = parse_entry(item)
        except DesiredError as exc:
            refused[label] = str(exc)
            continue
        if any(entry.name == other.name for other in usable):
            refused[label] = f"{entry.name}: listed twice"
            continue
        usable.append(entry)
        if credential is not None:
            credentials[entry.name] = credential
    return usable, credentials, refused
