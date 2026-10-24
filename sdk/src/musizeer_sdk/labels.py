"""
A provider image describes itself: the essentials of its manifest travel as the
labels ``org.musizeer.provider.*`` of the image, so whoever pulls it (the
gateway) can verify, before running anything, that it is what the catalog said
and see the permissions it asks for.

``labels_for(manifest)`` is what the image build puts in (``python -m
musizeer_sdk.runner --provider module:Class --print-labels``) and
``declared_from_labels(labels)`` what the gateway reads back, standard library
only. The label values are plain text: a name, comma separated roles, the
contract version and the permissions as JSON. The display name travels too (``label``), only so that
a development catalog can be written from an image; nothing verifies it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .manifest import ROLES, Manifest
from .permissions import Permissions, PermissionsError, from_json, to_json

LABEL_PREFIX = "org.musizeer.provider."
LABEL_NAME = LABEL_PREFIX + "name"
LABEL_ROLES = LABEL_PREFIX + "roles"
LABEL_API_VERSION = LABEL_PREFIX + "api-version"
LABEL_PERMISSIONS = LABEL_PREFIX + "permissions"
LABEL_LABEL = LABEL_PREFIX + "label"  # the name to show; optional (the gateway ignores it), a catalog entry is made from it
# The SDK the image was built on (ADR-0007 §3.1); optional: an image built before 5.5 has none.
LABEL_SDK_VERSION = LABEL_PREFIX + "sdk-version"

REQUIRED_LABELS = (LABEL_NAME, LABEL_ROLES, LABEL_API_VERSION, LABEL_PERMISSIONS)


class LabelError(ValueError):
    """The labels of an image do not describe a provider."""


@dataclass(frozen=True)
class DeclaredManifest:
    """What an image says about the provider inside it."""

    name: str
    roles: tuple[str, ...]
    provider_api_version: int
    permissions: Permissions


def labels_for(manifest: Manifest) -> dict[str, str]:
    if manifest.provider_api_version is None:
        raise LabelError(f"{manifest.name} does not declare 'provider_api_version'")
    labels = {
        LABEL_NAME: manifest.name,
        LABEL_ROLES: ",".join(manifest.roles),
        LABEL_API_VERSION: str(manifest.provider_api_version),
        LABEL_PERMISSIONS: to_json(manifest.permissions),
        LABEL_LABEL: manifest.label,
    }
    if manifest.sdk_version:
        labels[LABEL_SDK_VERSION] = manifest.sdk_version
    return labels


def declared_from_labels(labels: Mapping[str, str] | None) -> DeclaredManifest:
    labels = labels or {}
    missing = [key for key in REQUIRED_LABELS if key not in labels]
    if missing:
        raise LabelError("the image lacks the labels " + ", ".join(missing))
    roles = tuple(role for role in labels[LABEL_ROLES].split(",") if role)
    unknown = [role for role in roles if role not in ROLES]
    if not roles or unknown:
        raise LabelError(f"the image declares the roles {labels[LABEL_ROLES]!r}")
    try:
        version = int(labels[LABEL_API_VERSION])
    except ValueError:
        raise LabelError(f"'{labels[LABEL_API_VERSION]}' is not a provider API version") from None
    try:
        permissions = from_json(labels[LABEL_PERMISSIONS])
    except PermissionsError as exc:
        raise LabelError(str(exc)) from exc
    return DeclaredManifest(labels[LABEL_NAME], roles, version, permissions)
