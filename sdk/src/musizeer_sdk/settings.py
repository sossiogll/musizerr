"""
What a provider declares about its own settings: the data of
``BaseProvider.config_schema()``. The host turns each entry into its own
setting (namespaced under the provider, in the "providers" area, written by
the system scope): a provider never states where its settings live, who may
write them or how they are grouped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ProviderSettingType = Literal["bool", "int", "float", "string", "enum", "list", "folder"]


@dataclass(frozen=True, kw_only=True)
class ProviderSetting:
    """One setting of a provider. ``key`` is the short key the provider reads
    back with ``ProviderContext.setting(key)``.

    **A ``folder`` setting** is a place on the machine the administrator chooses as a root folder plus a path inside
    it (ADR-0008): the administrator never types an absolute path and the choice cannot leave its root. The provider
    still RECEIVES a plain absolute path string from ``setting(key)`` (the host resolves root and path before it sends
    the call's context), and never learns that roots exist. A folder setting has no default root: ``default`` is
    only the SUGGESTED path inside the root the administrator picks (a relative, normalized path such as
    ``"Library"``; ``None`` or ``""`` for the root itself), never an absolute one. Until the administrator sets it the
    provider receives no value for the key (``setting(key)`` is ``None``): it must answer "not configured", not guess.
    A setting named in ``Permissions.filesystem_from_settings`` must be a ``folder`` setting."""

    key: str
    type: ProviderSettingType
    label: str
    default: Any = None
    description: str = ""
    # For ``type == "enum"``: the accepted values.
    options: tuple[str, ...] | None = None
    # Optional bounds of a number (and the step a UI moves it by).
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    # True for a secret: the UI never shows it back.
    secret: bool = False
    # (another setting of THIS provider, by its short key, and the value it must
    # have) pairs that must ALL hold for this setting to be shown; the host
    # namespaces the keys like the setting's own.
    visible_when: tuple[tuple[str, Any], ...] | None = None
    # False for an optional setting: the provider works without a value.
    required: bool = True
