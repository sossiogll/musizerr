"""
The manifest: what a provider declares about itself, as plain data.

The host never looks at a provider's class. It asks the provider's runner to
``describe`` it once, when the provider is loaded, and reads this: the name,
the label, the ROLES the provider fills (which of the abstract classes of
``musizeer_sdk.interfaces`` it implements), its auth declaration, its settings,
every capability it declares (``supports_*()``, ``streams_full_tracks``,
``supported_identifier_types``...) and which of the optional methods it really
implements.

A capability is declared, never inferred: ``DECLARED_ATTRIBUTES`` and
``DECLARED_QUERIES`` are the closed list of what is read from the provider
class once and answered from here afterwards (a query is a method without
arguments whose answer does not change while the provider is loaded). Every
other method of the contract is a call.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

from . import wire
from .auth import AuthDefinition
from .interfaces import (
    BaseProvider,
    DownloadProvider,
    IdentificationProvider,
    LyricsProvider,
    MetadataProvider,
    StorageProvider,
    SyncProvider,
)
from .permissions import Permissions, validate as validate_permissions
from .settings import ProviderSetting
from .version import SDK_VERSION

# The kinds of provider, by the name the host files them under, in a stable order.
ROLES: dict[str, type[BaseProvider]] = {
    "metadata": MetadataProvider,
    "download": DownloadProvider,
    "lyrics": LyricsProvider,
    "storage": StorageProvider,
    "sync": SyncProvider,
    "identification": IdentificationProvider,
}

# Attributes of the class, read once.
DECLARED_ATTRIBUTES: dict[type, tuple[str, ...]] = {
    MetadataProvider: ("streams_full_tracks",),
    DownloadProvider: ("supported_identifier_types", "enabled_by_default", "max_concurrent_downloads", "supports_fuzzy"),
    StorageProvider: ("supports_stream_offset",),
}

# Methods without arguments, asked once.
DECLARED_QUERIES: dict[type, tuple[str, ...]] = {
    BaseProvider: ("supports_login", "is_library_available"),
    MetadataProvider: (
        "supports_discography",
        "supports_user_library",
        "supports_unlike_saved_track",
        "restricts_playlist_tracks",
        "supports_isrc_lookup",
    ),
    SyncProvider: ("supports_likes", "supports_playlists"),
}


class ManifestError(ValueError):
    """A provider that cannot be described (it lacks a name, a label, an auth
    declaration, or it fills no role)."""


@wire.register
@dataclass(frozen=True)
class Manifest:
    # The version of the contract the provider's package declares
    # (``provider_api_version``); None for a provider that does not declare one.
    provider_api_version: int | None
    name: str
    label: str
    roles: tuple[str, ...]
    auth: AuthDefinition
    config_schema: tuple[ProviderSetting, ...]
    # The answers to DECLARED_ATTRIBUTES / DECLARED_QUERIES of the roles it fills.
    declarations: dict[str, Any] = field(default_factory=dict)
    # The optional methods (the non-abstract ones of the contract) it overrides.
    implements: tuple[str, ...] = ()
    # What the sandbox grants it (an empty declaration: nothing).
    permissions: Permissions = field(default_factory=Permissions)
    # The SDK the provider was built on (ADR-0007 §3.1): the host encodes its calls for it. A provider that
    # says nothing is treated as BASELINE (6.0).
    sdk_version: str | None = None


def roles_of(provider: object) -> tuple[str, ...]:
    """The roles ``provider`` (an instance or a class) fills."""
    cls = provider if isinstance(provider, type) else type(provider)
    return tuple(role for role, base in ROLES.items() if issubclass(cls, base))


def _bases(roles: tuple[str, ...]) -> list[type]:
    return [BaseProvider, *(ROLES[role] for role in roles)]


def contract_methods(provider: object) -> frozenset[str]:
    """The public methods of the contract for the roles ``provider`` fills: the
    only names a runner will call."""
    names: set[str] = set()
    for base in _bases(roles_of(provider)):
        names.update(name for name, member in vars(base).items() if not name.startswith("_") and inspect.isfunction(member))
    return frozenset(names)


def declared_names(roles: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(attributes, queries) the manifest of a provider with ``roles`` carries."""
    attributes: list[str] = []
    queries: list[str] = []
    for base in _bases(roles):
        attributes.extend(DECLARED_ATTRIBUTES.get(base, ()))
        queries.extend(DECLARED_QUERIES.get(base, ()))
    return tuple(attributes), tuple(queries)


def implemented_methods(provider: object) -> tuple[str, ...]:
    """The methods of the contract ``provider`` really implements: the abstract
    ones, and the optional ones it overrides (on its class or, for a test
    double, on the instance)."""
    cls = type(provider)
    own = vars(provider) if hasattr(provider, "__dict__") else {}
    found: set[str] = set()
    for base in _bases(roles_of(provider)):
        for name, member in vars(base).items():
            if name.startswith("_") or not inspect.isfunction(member):
                continue
            if name in own or getattr(cls, name, member) is not member:
                found.add(name)
    return tuple(sorted(found))


def build(provider: BaseProvider) -> Manifest:
    """Describe ``provider`` (an instance) as a :class:`Manifest`."""
    roles = roles_of(provider)
    if not roles:
        raise ManifestError(f"{type(provider).__name__} fills no provider role")
    for required in ("name", "label", "auth"):
        if not hasattr(provider, required):
            raise ManifestError(f"{type(provider).__name__} does not declare '{required}'")
    if not isinstance(provider.auth, AuthDefinition):
        raise ManifestError(f"{provider.name}: 'auth' is not an AuthDefinition")
    attributes, queries = declared_names(roles)
    declarations: dict[str, Any] = {}
    for name in attributes:
        declarations[name] = getattr(provider, name)
    for name in queries:
        declarations[name] = getattr(provider, name)()
    for name, value in declarations.items():
        if isinstance(value, set):
            declarations[name] = frozenset(value)
    return Manifest(
        provider_api_version=getattr(provider, "provider_api_version", None),
        name=provider.name,
        label=provider.label,
        roles=roles,
        auth=provider.auth,
        config_schema=tuple(provider.config_schema()),
        declarations=declarations,
        implements=implemented_methods(provider),
        permissions=validate_permissions(getattr(provider, "permissions", Permissions())),
        sdk_version=SDK_VERSION,
    )

