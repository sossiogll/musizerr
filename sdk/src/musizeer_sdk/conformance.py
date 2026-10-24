"""
The generic rules every provider has to follow, as checks a provider's own test
suite runs on its provider instance. Standard library only, like the rest of
the SDK.

The host cannot check them for you: it never holds a provider, and the
providers live in repositories of their own. So the rules that were once
checked "over every provider" in the host's tests are here, and each provider's
repository calls them::

    from musizeer_sdk.context import StaticProviderContext
    from musizeer_sdk.conformance import check_provider

    def test_my_provider_follows_the_contract():
        check_provider(MyProvider(StaticProviderContext()))

``check_provider`` runs every rule and raises ONE ``ConformanceError`` (an
``AssertionError``, so a test runner shows it as a failed assertion) that lists
every violation, not just the first. The individual ``check_*`` functions run a
single rule; the ``*_violations`` functions return the list instead of
raising, for a test that wants to look at it.

The rules:

* ``config_schema()`` is well formed (``config_schema_violations``): a list of
  ``ProviderSetting``; every key a non-empty string and unique; the type one
  the host knows (``bool``, ``int``, ``float``, ``string``, ``enum``,
  ``list``, ``folder``); an ``enum`` declares its ``options`` and its default is one of
  them; a default has the type it declares; the bounds of a number do not
  cross; ``visible_when`` names other settings of the same provider (and, for
  an ``enum``, a value it can have).
* Folders (``folder_violations``, ADR-0008): a ``folder`` setting is not ``secret`` and its default is ``None`` or a
  relative, normalized path (the SUGGESTED path inside the root the administrator picks, never an absolute one); every
  setting named by ``Permissions.filesystem_from_settings`` is a declared ``folder`` setting; the setting a storage
  provider names with ``library_folder_setting()`` is a declared ``folder`` setting.
* An OAuth2 provider maps the capabilities that need a scope
  (``oauth2_capability_violations``): a provider that declares a capability
  that writes on the user's account (``CAPABILITIES_NEEDING_A_SCOPE``) has the
  scopes it needs in ``OAuth2Config.capability_scopes``, which is what lets the
  host tell a user who connected before the capability existed to reconnect.
  A mapping must also name a capability the provider declares.
"""
from __future__ import annotations

import typing
from typing import Any

from musizeer_sdk import capabilities
from musizeer_sdk.auth import AuthDefinition, AuthFlow
from musizeer_sdk.folders import relative_path_problem
from musizeer_sdk.interfaces import StorageProvider
from musizeer_sdk.permissions import NO_PERMISSIONS
from musizeer_sdk.settings import ProviderSetting, ProviderSettingType

SETTING_TYPES: frozenset[str] = frozenset(typing.get_args(ProviderSettingType))

# The ``supports_*`` query a provider answers to declare each capability
# (``musizeer_sdk.capabilities``), in the order the host lists them.
CAPABILITY_DECLARATIONS: tuple[tuple[str, str], ...] = (
    (capabilities.CAPABILITY_USER_LIBRARY, "supports_user_library"),
    (capabilities.CAPABILITY_UNLIKE_SAVED_TRACK, "supports_unlike_saved_track"),
    (capabilities.CAPABILITY_DISCOGRAPHY, "supports_discography"),
    (capabilities.CAPABILITY_PLAYLIST_TRACKS_RESTRICTED, "restricts_playlist_tracks"),
    (capabilities.CAPABILITY_PROPAGATE_FAVORITES, "supports_likes"),
    (capabilities.CAPABILITY_PROPAGATE_PLAYLISTS, "supports_playlists"),
    (capabilities.CAPABILITY_LOGIN, "supports_login"),
)

# The capabilities that write on the user's own account of an OAuth2 service,
# and so ask for a scope a plain sign-in does not grant. An OAuth2 provider that
# declares one maps it in ``OAuth2Config.capability_scopes``.
CAPABILITIES_NEEDING_A_SCOPE: frozenset[str] = frozenset({
    capabilities.CAPABILITY_UNLIKE_SAVED_TRACK,
    capabilities.CAPABILITY_PROPAGATE_FAVORITES,
    capabilities.CAPABILITY_PROPAGATE_PLAYLISTS,
})

_KNOWN_CAPABILITIES: frozenset[str] = frozenset(
    value for name, value in vars(capabilities).items() if name.startswith("CAPABILITY_")
)


class ConformanceError(AssertionError):
    """A provider breaks one or more rules of the contract. ``violations`` is
    the list, one readable sentence each."""

    def __init__(self, provider: str, violations: list[str]) -> None:
        self.provider = provider
        self.violations = tuple(violations)
        lines = "\n".join(f"  - {violation}" for violation in violations)
        super().__init__(f"provider '{provider}' breaks {len(violations)} rule(s) of the contract:\n{lines}")


def _name(provider: Any) -> str:
    return str(getattr(provider, "name", None) or type(provider).__name__)


def _answers_yes(provider: Any, method: str) -> bool:
    query = getattr(provider, method, None)
    return callable(query) and bool(query())


def declared_capabilities(provider: Any) -> list[str]:
    """The capability keys ``provider`` declares with its ``supports_*``
    queries, in a stable order (the host's own list, not limited to the roles
    the provider fills)."""
    return [key for key, method in CAPABILITY_DECLARATIONS if _answers_yes(provider, method)]


# --- config_schema() -----------------------------------------------------------


def config_schema_violations(provider: Any) -> list[str]:
    """What is wrong with ``provider.config_schema()``, one sentence each; an
    empty list when it is well formed."""
    try:
        schema = provider.config_schema()
    except Exception as exc:  # noqa: BLE001 - the rule is that it can be read
        return [f"config_schema() raised {type(exc).__name__}: {exc}"]
    if not isinstance(schema, list):
        return [f"config_schema() must return a list, it returned {type(schema).__name__}"]

    problems: list[str] = []
    entries = [entry for entry in schema if isinstance(entry, ProviderSetting)]
    for entry in schema:
        if not isinstance(entry, ProviderSetting):
            problems.append(f"config_schema() entries must be ProviderSetting, found {type(entry).__name__}")

    by_key: dict[str, ProviderSetting] = {}
    for entry in entries:
        where = f"setting '{entry.key}'"
        if not isinstance(entry.key, str) or not entry.key.strip():
            problems.append(f"a setting has an empty or non-string key ({entry.key!r})")
            continue
        if entry.key in by_key:
            problems.append(f"{where} is declared more than once")
            continue
        by_key[entry.key] = entry
        problems.extend(_setting_violations(entry, where))

    for entry in by_key.values():
        problems.extend(_visible_when_violations(entry, by_key))
    return problems


def _setting_violations(entry: ProviderSetting, where: str) -> list[str]:
    problems: list[str] = []
    if entry.type not in SETTING_TYPES:
        problems.append(f"{where} has the type {entry.type!r}, not one of {sorted(SETTING_TYPES)}")
        return problems
    if not isinstance(entry.label, str) or not entry.label.strip():
        problems.append(f"{where} has no label")

    if entry.type == "enum":
        options = entry.options
        if not options:
            problems.append(f"{where} is an enum and declares no options")
        else:
            if not all(isinstance(option, str) and option for option in options):
                problems.append(f"{where} has options that are not non-empty strings")
            if len(set(options)) != len(options):
                problems.append(f"{where} lists an option more than once")
            if entry.default is not None and entry.default not in options:
                problems.append(f"{where} has the default {entry.default!r}, which is not one of its options")
    elif entry.default is not None and not _has_type(entry.default, entry.type):
        problems.append(f"{where} is a {entry.type} and its default is {entry.default!r}")

    if entry.minimum is not None and entry.maximum is not None and entry.minimum > entry.maximum:
        problems.append(f"{where} has a minimum ({entry.minimum}) above its maximum ({entry.maximum})")
    if entry.step is not None and entry.step <= 0:
        problems.append(f"{where} has a step that is not positive ({entry.step})")
    return problems


def _has_type(value: Any, setting_type: str) -> bool:
    if setting_type == "bool":
        return isinstance(value, bool)
    if setting_type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if setting_type == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if setting_type == "string":
        return isinstance(value, str)
    if setting_type == "list":
        return isinstance(value, (list, tuple))
    if setting_type == "folder":
        return isinstance(value, str)
    return True


def _visible_when_violations(entry: ProviderSetting, by_key: dict[str, ProviderSetting]) -> list[str]:
    if entry.visible_when is None:
        return []
    where = f"setting '{entry.key}'"
    problems: list[str] = []
    for condition in entry.visible_when:
        if not isinstance(condition, (tuple, list)) or len(condition) != 2:
            problems.append(f"{where} has a visible_when condition that is not a (key, value) pair: {condition!r}")
            continue
        other_key, value = condition
        if other_key == entry.key:
            problems.append(f"{where} is shown only when itself has a value (visible_when names its own key)")
        elif other_key not in by_key:
            problems.append(f"{where} is shown only when '{other_key}' has a value, and the provider declares no such setting")
        else:
            other = by_key[other_key]
            if other.type == "enum" and other.options and value not in other.options:
                problems.append(
                    f"{where} is shown only when '{other_key}' is {value!r}, which is not one of its options "
                    "(it could never be shown)"
                )
    return problems


# --- folders (ADR-0008) ------------------------------------------------------------


def _declared_settings(provider: Any) -> dict[str, ProviderSetting]:
    """The well formed entries of ``config_schema()`` by key (what is wrong with the schema itself is
    ``config_schema_violations``' to say); empty when it cannot be read."""
    try:
        schema = provider.config_schema()
    except Exception:  # noqa: BLE001 - reported by config_schema_violations
        return {}
    if not isinstance(schema, list):
        return {}
    return {entry.key: entry for entry in schema if isinstance(entry, ProviderSetting) and isinstance(entry.key, str)}


def folder_violations(provider: Any) -> list[str]:
    """What is wrong with the way a provider uses ``folder`` settings; an empty list when it is fine.

    A ``folder`` setting has no default root: its ``default`` is only a relative, normalized path to suggest inside
    the root the administrator picks, and it is never a secret. A folder is granted to the sandbox through
    ``filesystem_from_settings`` and ONLY a ``folder`` setting is one the host can resolve against the declared roots,
    so every setting named there has to be one. The setting a storage provider declares as its library
    (``library_folder_setting()``) has to be one too."""
    settings = _declared_settings(provider)
    problems: list[str] = []

    for key, entry in settings.items():
        if entry.type != "folder":
            continue
        where = f"setting '{key}'"
        if entry.secret:
            problems.append(f"{where} is a folder and is declared secret (a folder is shown back to the administrator)")
        if entry.default is not None:
            problem = relative_path_problem(entry.default)
            if problem is not None:
                problems.append(
                    f"{where} is a folder whose default {entry.default!r} {problem}: the default of a folder is only "
                    "the suggested path inside the root folder, relative and normalized, or None"
                )

    permissions = getattr(provider, "permissions", NO_PERMISSIONS)
    for access in getattr(permissions, "filesystem_from_settings", ()):
        declared = settings.get(access.setting)
        if declared is None:
            problems.append(f"filesystem_from_settings names '{access.setting}', which the provider declares no such setting for")
        elif declared.type != "folder":
            problems.append(
                f"filesystem_from_settings names '{access.setting}', which is a {declared.type} setting: "
                "only a folder setting can be granted to the sandbox (type='folder')"
            )

    if isinstance(provider, StorageProvider):
        key = provider.library_folder_setting()
        if key is not None:
            declared = settings.get(key)
            if declared is None:
                problems.append(f"library_folder_setting() names '{key}', which the provider declares no setting for")
            elif declared.type != "folder":
                problems.append(f"library_folder_setting() names '{key}', which is a {declared.type} setting, not a folder")
    return problems


# --- OAuth2 capability scopes ----------------------------------------------------


def oauth2_capability_violations(provider: Any) -> list[str]:
    """What is wrong with the way an OAuth2 provider maps its capabilities to
    scopes; an empty list for a provider that is not OAuth2 or maps them all."""
    auth = getattr(provider, "auth", None)
    if not isinstance(auth, AuthDefinition) or auth.flow != AuthFlow.OAUTH2:
        return []
    config = auth.oauth2
    if config is None:
        return ["the auth flow is OAUTH2 and the provider gives no oauth2 configuration"]

    problems: list[str] = []
    declared = declared_capabilities(provider)
    mapped = config.capability_scopes

    for key in declared:
        if key in CAPABILITIES_NEEDING_A_SCOPE and not mapped.get(key):
            problems.append(
                f"it declares the capability '{key}' and OAuth2Config.capability_scopes maps no scope for it "
                "(a connection made before the scope existed could never be told to reconnect)"
            )

    for key, scopes in mapped.items():
        if key not in _KNOWN_CAPABILITIES:
            problems.append(f"OAuth2Config.capability_scopes maps '{key}', which is not a capability of the contract")
        elif key not in declared:
            problems.append(
                f"OAuth2Config.capability_scopes maps scopes for '{key}', which the provider does not declare "
                "(every new connection would ask the user for a permission nothing uses)"
            )
        if not isinstance(scopes, (list, tuple)) or not scopes or not all(isinstance(s, str) and s for s in scopes):
            problems.append(f"OAuth2Config.capability_scopes['{key}'] must be a non-empty list of scope names")
    return problems


# --- the checks -------------------------------------------------------------------


def _raise(provider: Any, violations: list[str]) -> None:
    if violations:
        raise ConformanceError(_name(provider), violations)


def check_config_schema(provider: Any) -> None:
    """Raises ``ConformanceError`` unless ``provider.config_schema()`` is well formed."""
    _raise(provider, config_schema_violations(provider))


def check_folders(provider: Any) -> None:
    """Raises ``ConformanceError`` if the provider uses ``folder`` settings wrongly (ADR-0008)."""
    _raise(provider, folder_violations(provider))


def check_oauth2_capability_scopes(provider: Any) -> None:
    """Raises ``ConformanceError`` if an OAuth2 provider does not map the
    capabilities it declares that need a scope."""
    _raise(provider, oauth2_capability_violations(provider))


def provider_violations(provider: Any) -> list[str]:
    """Every violation of every rule, in one list."""
    return [*config_schema_violations(provider), *folder_violations(provider), *oauth2_capability_violations(provider)]


def check_provider(provider: Any) -> None:
    """Runs every rule on ``provider`` (an instance, built with a
    ``StaticProviderContext``) and raises ONE ``ConformanceError`` listing
    every violation."""
    _raise(provider, provider_violations(provider))
