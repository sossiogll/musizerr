"""
Conformità di ``BaseProvider.config_schema()`` per i provider registrati nel
``ProviderRegistry`` (uno per categoria, finti: ``tests/settings_support.py``) e
della sua traduzione in ``SettingDef`` da parte dell'host. Quanto ogni provider
reale dichiara ben formato lo schema è nei test del suo repository.

Regola d'oro rispettata: il test itera genericamente sul registry — nessuna
asserzione nomina un provider concreto. Il provider dichiara solo i dati di
``ProviderSetting``; chiave namespaced, gruppo, area e governance li decide
l'host (``settings_resolver.provider_setting_def``).
"""

from __future__ import annotations

import dataclasses

import pytest

from core import settings_resolver as sr
from core.settings_catalog import AREA_PROVIDERS, SettingDef
from musizeer_sdk.interfaces import BaseProvider
from musizeer_sdk.settings import ProviderSetting
from services.provider_registry import ProviderRegistry
from tests.settings_support import registry_of_every_category

_ALLOWED_TYPES = {"bool", "int", "float", "string", "enum", "list"}


@pytest.fixture(scope="module")
def registry() -> ProviderRegistry:
    return registry_of_every_category()


def _all_providers(registry: ProviderRegistry) -> list[BaseProvider]:
    """Tutti i provider registrati, deduplicati per identità: un SyncProvider
    che è anche MetadataProvider (es. un provider duale come Navidrome) viene
    cross-registrato in entrambe le categorie dal registry — va controllato
    una volta sola, non due."""
    seen: dict[int, BaseProvider] = {}
    for getter in (
        registry.all_metadata,
        registry.all_download,
        registry.all_lyrics,
        registry.all_storage,
        registry.all_sync,
        registry.all_identification,
    ):
        for p in getter():
            seen[id(p)] = p
    return list(seen.values())


def test_registry_has_providers(registry):
    # Sanity: se la scoperta reale non trova nulla, il test sotto passerebbe
    # a vuoto senza verificare niente — meglio fallire rumorosamente.
    assert _all_providers(registry), "nessun provider scoperto dal ProviderRegistry reale"


def test_every_registered_provider_config_schema_is_well_formed(registry):
    for provider in _all_providers(registry):
        schema = provider.config_schema()
        assert isinstance(schema, list), f"{provider.name}: config_schema() deve restituire una list"

        seen_keys: set[str] = set()
        for entry in schema:
            assert isinstance(entry, ProviderSetting), (
                f"{provider.name}: ogni voce dello schema deve essere un ProviderSetting, trovato {type(entry)!r}"
            )
            assert entry.key != "", f"{provider.name}: chiave vuota non ammessa"
            assert entry.key not in seen_keys, f"{provider.name}: chiave duplicata '{entry.key}' nello schema"
            seen_keys.add(entry.key)

            assert entry.type in _ALLOWED_TYPES, (
                f"{provider.name}.{entry.key}: type '{entry.type}' non è tra i tipi ammessi"
            )
            if entry.type == "enum":
                assert entry.options, (
                    f"{provider.name}.{entry.key}: un'impostazione enum deve dichiarare options non vuote"
                )


def test_the_host_places_every_provider_setting_under_its_provider(registry):
    """Quello che il provider non dichiara lo decide l'host: chiave nel
    namespace del provider, gruppo = nome del provider, area "providers",
    scope di sistema, mai nascosta e senza requisiti."""
    merged = {s.key: s for s in sr.merged_schema(registry)}

    for provider in _all_providers(registry):
        for declared in provider.config_schema():
            setting = merged[sr.namespace_provider_key(provider.name, declared.key)]
            assert (setting.group, setting.area, setting.scope) == (provider.name, AREA_PROVIDERS, "system")
            assert (setting.hidden, setting.requires) == (False, ())


def test_provider_setting_def_carries_every_declared_field_and_forces_the_host_ones():
    declared = ProviderSetting(
        key="mode", type="enum", default="a", label="Mode", description="d", options=("a", "b"),
        minimum=1, maximum=9, step=2, secret=True, visible_when=(("other", "x"),), required=False,
    )

    setting = sr.provider_setting_def("some-provider", declared)

    assert setting == SettingDef(
        key="provider.some-provider.mode", type="enum", default="a", group="some-provider", label="Mode",
        description="d", area=AREA_PROVIDERS, options=("a", "b"), minimum=1, maximum=9, step=2, secret=True,
        visible_when=(("provider.some-provider.other", "x"),), required=False,
    )


def test_provider_setting_def_leaves_nothing_of_the_declaration_behind():
    """Ogni campo di ProviderSetting arriva nella SettingDef (un campo nuovo
    del contratto non può andare perso in silenzio)."""
    names = {f.name for f in dataclasses.fields(ProviderSetting)}
    declared = ProviderSetting(
        key="k", type="string", default="d", label="L", description="desc", options=("o",), minimum=0, maximum=1,
        step=1, secret=True, visible_when=(("a", 1),), required=False,
    )

    setting = sr.provider_setting_def("p", declared)

    for name in names - {"key", "visible_when"}:
        assert getattr(setting, name) == getattr(declared, name), name


def test_the_keys_a_provider_setting_is_visible_when_refers_to_are_namespaced_like_its_own_key():
    """Un provider rimanda con visible_when alle SUE short key: nello schema
    dell'app, dove le chiavi del provider sono namespaced, il riferimento deve
    puntare alla chiave namespaced, altrimenti non troverebbe mai nulla."""
    declared = ProviderSetting(
        key="url", type="string", label="URL", visible_when=(("mode", "full"), ("other", True)),
    )

    setting = sr.provider_setting_def("some-provider", declared)

    assert setting.visible_when == (
        (sr.namespace_provider_key("some-provider", "mode"), "full"),
        (sr.namespace_provider_key("some-provider", "other"), True),
    )
    # Every referenced key is one of the provider's own settings once namespaced.
    assert all(split[0] == "some-provider" for split in (sr.split_provider_key(k) for k, _ in setting.visible_when))


def test_a_setting_visible_always_stays_so():
    assert sr.provider_setting_def("p", ProviderSetting(key="k", type="int", label="L")).visible_when is None


def test_a_provider_setting_is_frozen_and_keyword_only():
    declared = ProviderSetting(key="k", type="int", label="L")

    with pytest.raises(dataclasses.FrozenInstanceError):
        declared.key = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        ProviderSetting("k", "int", "L")  # type: ignore[misc]
