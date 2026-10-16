"""
Ciò che la UI delle impostazioni legge dal contratto invece di cablarlo:
quale valore chiede una conferma esplicita (``confirm``) e come si traduce
ogni opzione di un enum (``option_label_keys``).
"""
from __future__ import annotations

from core.settings_catalog import CATALOG, SettingDef


def _public(key: str) -> dict:
    return next(d for d in CATALOG if d.key == key).to_public()


def test_turning_authentication_off_asks_for_confirmation():
    confirm = _public("auth_enabled")["confirm"]

    assert confirm["value"] is False
    assert confirm["message_key"] == "settings.confirm.auth_enabled"
    assert confirm["message"]


def test_settings_without_a_risky_value_ask_nothing():
    assert _public("registration_open")["confirm"] is None


def test_every_enum_option_has_a_translation_key_in_order():
    enums = [d for d in CATALOG if d.options]
    assert enums
    for definition in enums:
        public = definition.to_public()
        assert public["option_label_keys"] == [
            f"settings.options.{definition.key}.{option}" for option in definition.options
        ]


def test_a_setting_without_options_has_no_label_keys():
    plain = SettingDef("x", "int", 1, "g", "X")
    assert plain.to_public()["option_label_keys"] is None


def test_every_group_has_translation_keys_for_label_and_description():
    from core.settings_catalog import public_taxonomy
    for group in public_taxonomy()["groups"]:
        assert group["label_key"] == f"settings.groups.{group['key']}"
        assert group["description_key"] == f"settings.group_descriptions.{group['key']}"
