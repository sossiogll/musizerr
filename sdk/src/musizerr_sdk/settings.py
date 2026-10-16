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

ProviderSettingType = Literal["bool", "int", "float", "string", "enum", "list"]


@dataclass(frozen=True, kw_only=True)
class ProviderSetting:
    """One setting of a provider. ``key`` is the short key the provider reads
    back with ``ProviderContext.setting(key)``."""

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
