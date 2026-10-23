"""Catalogo impostazioni: unica sorgente dei default, provider-agnostico."""

from __future__ import annotations

from core import settings_catalog as sc


def test_catalog_defaults_cover_every_entry():
    defaults = sc.catalog_defaults()
    assert set(defaults) == {s.key for s in sc.CATALOG}


def test_keys_are_unique():
    keys = [s.key for s in sc.CATALOG]
    assert len(keys) == len(set(keys))


def test_to_public_never_leaks_values():
    # SettingDef.to_public() (usato da GET /settings/schema) espone metadati
    # (tipo/gruppo/label), MAI il valore/default — l'unico endpoint di schema
    # rimasto dopo la rimozione di GET /config/schema e della sua
    # public_catalog() dedicata, quindi verificato direttamente su to_public().
    for entry_def in sc.CATALOG:
        entry = entry_def.to_public()
        assert "value" not in entry
        assert "default" not in entry
        assert {"key", "type", "group", "label"} <= set(entry)


def test_enum_settings_declare_options():
    for s in sc.CATALOG:
        if s.type == "enum":
            assert s.options, f"enum '{s.key}' senza options"
            assert s.default in s.options


def test_provider_agnostic_no_provider_is_named_in_the_defaults():
    # La Regola d'Oro: il catalogo non cabla nessun provider. Le impostazioni
    # che scelgono provider (priorità, ordine, riferimento) partono vuote: è
    # l'utente, in config, a metterci i nomi dei provider che ha installato.
    # Nessuna eccezione: le installazioni che si affidavano al vecchio default
    # dello storage hanno il valore scritto dalla migrazione 0040.
    named = {s.key: s.default for s in sc.CATALOG if s.type in ("list", "provider_ref") and s.default}
    assert named == {}


def test_provider_agnostic_no_catalog_key_lives_in_the_namespace_of_a_provider():
    # La config specifica di un provider (es. la chiave di un servizio) va NEL
    # provider (config_schema), non nel catalogo core: nessuna chiave del
    # catalogo è nel namespace ``provider.<nome>.<chiave>``.
    from core import settings_resolver as sr

    for s in sc.CATALOG:
        assert sr.split_provider_key(s.key)[0] is None, f"la chiave '{s.key}' è nel namespace di un provider"


def test_numeric_constraints_are_consistent():
    for s in sc.CATALOG:
        if s.minimum is not None and s.maximum is not None:
            assert s.minimum <= s.maximum
        if s.type == "int" and isinstance(s.default, int):
            if s.minimum is not None:
                assert s.default >= s.minimum
            if s.maximum is not None:
                assert s.default <= s.maximum


def test_get_def_roundtrip():
    for s in sc.CATALOG:
        assert sc.get_def(s.key) is s
    assert sc.get_def("__does_not_exist__") is None


def test_the_default_workspace_folder_follows_the_variable_the_deployment_shares_with_the_gateway(monkeypatch):
    monkeypatch.delenv("WORKSPACE_DIR", raising=False)
    assert sc.default_workspace_dir() == "/downloads/.musizerr-work"

    monkeypatch.setenv("WORKSPACE_DIR", "  /library/.work ")
    assert sc.default_workspace_dir() == "/library/.work"

    monkeypatch.setenv("WORKSPACE_DIR", "")
    assert sc.default_workspace_dir() == "/downloads/.musizerr-work", "an empty variable is no variable"
