"""
Fase 3a: motore di risoluzione delle impostazioni (``core/settings_resolver``).

Regola d'oro rispettata: i fake vivono qui, nessun nome di provider concreto
compare in un'asserzione. Dove serve provare la deduplicazione multi-categoria
sul registry REALE (un provider dell'app è deliberatamente registrato sia
come sync sia come metadata provider), si asserisce la proprietà strutturale
("qualche provider compare in più categorie e contribuisce una sola volta"),
mai il nome di quel provider.
"""

from __future__ import annotations

import pytest

from core import settings_catalog as sc
from core import settings_resolver as sr
from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizeer_sdk.interfaces import MetadataProvider, SyncProvider
from core.settings_catalog import SettingDef
from musizeer_sdk.settings import ProviderSetting
from tests.settings_support import registry_of_every_category

from tests.conftest import FakeMetadataProvider, FakeStorageProvider, FakeSyncProvider
from tests.loopback import as_proxy


# ---------------------------------------------------------------------------
# Fake locali
# ---------------------------------------------------------------------------


class _SettingsMetadataProvider(FakeMetadataProvider):
    """Metadata provider finto che dichiara uno schema esplicito."""

    def __init__(self, name: str, defs: list[ProviderSetting]) -> None:
        super().__init__(name)
        self._defs = defs

    def config_schema(self) -> list[ProviderSetting]:
        return list(self._defs)


class _DualMetadataSyncProvider(MetadataProvider, SyncProvider):
    """Provider duale (metadata + sync) finto: simula la cross-registrazione
    che il ProviderRegistry reale applica a un provider duale — STESSO oggetto
    presente in due categorie."""

    name = "dualtestprovider"
    label = "Dual Test Provider"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, defs: list[ProviderSetting]) -> None:
        self._defs = defs

    def config_schema(self) -> list[ProviderSetting]:
        return list(self._defs)

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session) -> str | None:
        return None

    def trigger_scan(self, session) -> None:
        return None

    def find_candidates(self, track, session):
        return []

    def get_stream_url(self, sync_id, session) -> str | None:
        return None


class _FakeRegistry:
    """Registry fittizio: implementa la parte del Protocol ProviderRegistry
    usata da settings_resolver (i sei ``all_*`` più ``names_for_category``).
    Nessun provider reale compare mai qui."""

    def __init__(
        self,
        *,
        metadata=None,
        download=None,
        lyrics=None,
        storage=None,
        sync=None,
        identification=None,
    ) -> None:
        self._by_category = {
            category: [as_proxy(p) for p in providers or []]
            for category, providers in (
                ("metadata", metadata), ("download", download), ("lyrics", lyrics),
                ("storage", storage), ("sync", sync), ("identification", identification),
            )
        }

    def all_metadata(self):
        return list(self._by_category["metadata"])

    def all_download(self):
        return list(self._by_category["download"])

    def all_lyrics(self):
        return list(self._by_category["lyrics"])

    def all_storage(self):
        return list(self._by_category["storage"])

    def all_sync(self):
        return list(self._by_category["sync"])

    def all_identification(self):
        return list(self._by_category["identification"])

    def names_for_category(self, category: str) -> list[str]:
        return [p.name for p in self._by_category.get(category, [])]


# ---------------------------------------------------------------------------
# Namespacing: round trip
# ---------------------------------------------------------------------------


def test_namespace_provider_key_shape():
    assert sr.namespace_provider_key("acme", "search_limit") == "provider.acme.search_limit"


def test_split_provider_key_roundtrip():
    key = sr.namespace_provider_key("acme", "search_limit")
    provider_name, short_key = sr.split_provider_key(key)
    assert provider_name == "acme"
    assert short_key == "search_limit"


def test_split_provider_key_roundtrip_with_dot_in_short_key():
    # La short key stessa contiene un punto: deve sopravvivere intatta.
    key = sr.namespace_provider_key("acme", "nested.sub_key")
    provider_name, short_key = sr.split_provider_key(key)
    assert provider_name == "acme"
    assert short_key == "nested.sub_key"
    # E il round trip nell'altro verso deve ricostruire la stessa chiave.
    assert sr.namespace_provider_key(provider_name, short_key) == key


def test_split_provider_key_plain_app_level_key_has_no_provider():
    provider_name, short_key = sr.split_provider_key("search_default_limit")
    assert provider_name is None
    assert short_key == "search_default_limit"


# ---------------------------------------------------------------------------
# Schema unificato
# ---------------------------------------------------------------------------


def test_merged_schema_starts_with_catalog_unchanged():
    merged = sr.merged_schema(_FakeRegistry())
    assert merged[: len(sc.CATALOG)] == list(sc.CATALOG)


def test_merged_schema_namespaces_provider_settings_without_collision():
    # Due provider diversi che dichiarano DELIBERATAMENTE la stessa short key
    # (com'è oggi nel repo per "search_limit"/"url" su più provider reali).
    prov_a = _SettingsMetadataProvider(
        "provalpha", [ProviderSetting(key="search_limit", type="int", default=20, label="Limit")]
    )
    prov_b = _SettingsMetadataProvider(
        "provbeta", [ProviderSetting(key="search_limit", type="int", default=30, label="Limit")]
    )
    registry = _FakeRegistry(metadata=[prov_a, prov_b])

    merged = sr.merged_schema(registry)
    keys = [s.key for s in merged]

    assert sr.namespace_provider_key("provalpha", "search_limit") in keys
    assert sr.namespace_provider_key("provbeta", "search_limit") in keys
    assert len(keys) == len(set(keys)), "chiavi duplicate nello schema unificato"


def test_merged_schema_dedupes_provider_registered_in_two_categories():
    defs = [ProviderSetting(key="url", type="string", label="URL")]
    dual = _DualMetadataSyncProvider(defs)
    # Stessa IDENTITÀ passata sia come metadata sia come sync provider — esattamente
    # come il ProviderRegistry reale cross-registra un provider duale.
    registry = _FakeRegistry(metadata=[dual], sync=[dual])

    merged = sr.merged_schema(registry)
    namespaced = sr.namespace_provider_key(dual.name, "url")
    matches = [s for s in merged if s.key == namespaced]

    assert len(matches) == 1


def test_real_registry_multi_category_provider_contributes_settings_once():
    """Prova strutturale sul registry REALE: individua (per identità, non per
    nome) un provider registrato in più di una categoria e verifica che le
    sue impostazioni compaiano esattamente una volta nello schema unificato.
    Nessuna asserzione nomina un provider concreto."""
    registry = registry_of_every_category()

    providers_by_category = {
        "metadata": registry.all_metadata(),
        "download": registry.all_download(),
        "lyrics": registry.all_lyrics(),
        "storage": registry.all_storage(),
        "sync": registry.all_sync(),
        "identification": registry.all_identification(),
    }

    categories_by_id: dict[int, set[str]] = {}
    provider_by_id: dict[int, object] = {}
    for category, providers in providers_by_category.items():
        for p in providers:
            categories_by_id.setdefault(id(p), set()).add(category)
            provider_by_id[id(p)] = p

    dual_ids = [pid for pid, cats in categories_by_id.items() if len(cats) > 1]
    assert dual_ids, (
        "il test assume che il registry reale abbia almeno un provider "
        "registrato in più di una categoria (vedi cross-registrazione "
        "sync/metadata in services/provider_registry.py)"
    )

    merged = sr.merged_schema(registry)
    merged_keys = [s.key for s in merged]
    assert len(merged_keys) == len(set(merged_keys)), "chiavi duplicate nello schema unificato reale"

    for pid in dual_ids:
        provider = provider_by_id[pid]
        provider_keys = {sr.namespace_provider_key(provider.name, s.key) for s in provider.config_schema()}
        occurrences = sum(1 for k in merged_keys if k in provider_keys)
        assert occurrences == len(provider_keys), (
            "le impostazioni di un provider multi-categoria devono comparire "
            "esattamente una volta nello schema unificato"
        )


# ---------------------------------------------------------------------------
# Policy: tabella scope x ruolo, per intero
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope,role,expected",
    [
        ("system", "admin", True),
        ("system", "user", False),
        ("user", "admin", True),
        ("user", "user", True),
        ("system_with_user_override", "admin", True),
        ("system_with_user_override", "user", True),
    ],
)
def test_can_read_policy_table(scope, role, expected):
    setting = SettingDef("k", "string", "", "g", "L", scope=scope)
    assert sr.can_read(setting, role) is expected


@pytest.mark.parametrize(
    "scope,role,expected",
    [
        ("system", "admin", True),
        ("system", "user", False),
        ("user", "admin", True),
        ("user", "user", True),
        ("system_with_user_override", "admin", True),
        ("system_with_user_override", "user", True),
    ],
)
def test_can_write_policy_table(scope, role, expected):
    setting = SettingDef("k", "string", "", "g", "L", scope=scope)
    assert sr.can_write(setting, role) is expected


# ---------------------------------------------------------------------------
# Coercizione
# ---------------------------------------------------------------------------


def test_coerce_bool_accepts_real_bool_and_string_spellings():
    setting = SettingDef("k", "bool", False, "g", "L")
    assert sr.coerce_value(setting, True) is True
    assert sr.coerce_value(setting, False) is False
    for truthy in ("true", "True", "1", "yes", "on", "ON"):
        assert sr.coerce_value(setting, truthy) is True
    for falsy in ("false", "False", "0", "no", "off", "OFF"):
        assert sr.coerce_value(setting, falsy) is False


def test_coerce_bool_rejects_unrecognized_string():
    setting = SettingDef("k", "bool", False, "g", "L")
    with pytest.raises(sr.SettingError) as exc_info:
        sr.coerce_value(setting, "maybe")
    assert exc_info.value.key == "k"


def test_coerce_int_accepts_numeric_and_numeric_string():
    setting = SettingDef("k", "int", 0, "g", "L")
    assert sr.coerce_value(setting, 5) == 5
    coerced = sr.coerce_value(setting, "5")
    assert coerced == 5
    assert isinstance(coerced, int)


def test_coerce_int_rejects_non_numeric_string():
    setting = SettingDef("k", "int", 0, "g", "L")
    with pytest.raises(sr.SettingError):
        sr.coerce_value(setting, "not-a-number")


def test_coerce_int_rejects_non_integer_numeric_string():
    setting = SettingDef("k", "int", 0, "g", "L")
    with pytest.raises(sr.SettingError):
        sr.coerce_value(setting, "5.5")


def test_coerce_int_rejects_bool():
    setting = SettingDef("k", "int", 0, "g", "L")
    with pytest.raises(sr.SettingError):
        sr.coerce_value(setting, True)


def test_coerce_float_accepts_numeric_and_numeric_string():
    setting = SettingDef("k", "float", 0.0, "g", "L")
    assert sr.coerce_value(setting, "5.5") == 5.5
    assert sr.coerce_value(setting, 5) == 5.0


def test_coerce_float_rejects_non_numeric_string():
    setting = SettingDef("k", "float", 0.0, "g", "L")
    with pytest.raises(sr.SettingError):
        sr.coerce_value(setting, "nope")


def test_coerce_string_accepts_string_and_stringifies_scalar():
    setting = SettingDef("k", "string", "", "g", "L")
    assert sr.coerce_value(setting, "hello") == "hello"
    assert sr.coerce_value(setting, 5) == "5"
    assert sr.coerce_value(setting, 5.5) == "5.5"
    assert sr.coerce_value(setting, True) == "True"


def test_coerce_string_rejects_non_scalar():
    setting = SettingDef("k", "string", "", "g", "L")
    with pytest.raises(sr.SettingError):
        sr.coerce_value(setting, ["a", "b"])


def test_coerce_enum_and_provider_ref_accept_only_string():
    enum_setting = SettingDef("k", "enum", "a", "g", "L", options=("a", "b"))
    assert sr.coerce_value(enum_setting, "a") == "a"
    with pytest.raises(sr.SettingError):
        sr.coerce_value(enum_setting, 1)

    ref_setting = SettingDef("k2", "provider_ref", None, "g", "L", provider_category="storage")
    assert sr.coerce_value(ref_setting, "vault") == "vault"
    with pytest.raises(sr.SettingError):
        sr.coerce_value(ref_setting, 1)


def test_coerce_list_accepts_only_list():
    setting = SettingDef("k", "list", [], "g", "L")
    assert sr.coerce_value(setting, ["a", "b"]) == ["a", "b"]
    with pytest.raises(sr.SettingError):
        sr.coerce_value(setting, "a,b")


# ---------------------------------------------------------------------------
# Validazione
# ---------------------------------------------------------------------------


def test_validate_enum_rejects_value_outside_options():
    setting = SettingDef("k", "enum", "a", "g", "L", options=("a", "b"))
    sr.validate_value(setting, "a", _FakeRegistry())  # non deve sollevare
    with pytest.raises(sr.SettingError):
        sr.validate_value(setting, "z", _FakeRegistry())


def test_validate_numeric_bounds():
    setting = SettingDef("k", "int", 5, "g", "L", minimum=1, maximum=10)
    sr.validate_value(setting, 5, _FakeRegistry())  # dentro i limiti, non deve sollevare
    with pytest.raises(sr.SettingError):
        sr.validate_value(setting, 0, _FakeRegistry())
    with pytest.raises(sr.SettingError):
        sr.validate_value(setting, 11, _FakeRegistry())


def test_validate_provider_ref_rejects_nonexistent_provider():
    registry = _FakeRegistry(storage=[FakeStorageProvider("vault")])
    setting = SettingDef("k", "provider_ref", None, "g", "L", provider_category="storage", required=True)
    with pytest.raises(sr.SettingError):
        sr.validate_value(setting, "ghost", registry)


def test_validate_provider_ref_rejects_provider_of_wrong_category():
    registry = _FakeRegistry(storage=[FakeStorageProvider("vault")], sync=[FakeSyncProvider("sonic")])
    setting = SettingDef("k", "provider_ref", None, "g", "L", provider_category="storage", required=True)
    # "sonic" esiste, ma nella categoria sync, non storage.
    with pytest.raises(sr.SettingError):
        sr.validate_value(setting, "sonic", registry)
    # Controllo di salute: il nome giusto nella categoria giusta passa.
    sr.validate_value(setting, "vault", registry)


def test_validate_provider_ref_allows_empty_value_when_optional():
    setting = SettingDef("k", "provider_ref", None, "g", "L", provider_category="sync", required=False)
    registry = _FakeRegistry()
    sr.validate_value(setting, None, registry)
    sr.validate_value(setting, "", registry)


def test_validate_required_setting_rejects_empty_value():
    setting = SettingDef("k", "string", "x", "g", "L", required=True)
    with pytest.raises(sr.SettingError):
        sr.validate_value(setting, "", _FakeRegistry())
    with pytest.raises(sr.SettingError):
        sr.validate_value(setting, None, _FakeRegistry())


@pytest.mark.parametrize(
    "key,empty_value",
    [
        # sync_provider: la propria descrizione lo dice opzionale, il default
        # è None e la UI espone un pulsante "None" esplicito per disattivare
        # il sync — deve poter tornare vuoto.
        ("sync_provider", None),
        # public_base_url: default stringa vuota, serve solo alla
        # registrazione push, l'app funziona senza.
        ("public_base_url", ""),
        # delegation_webhook_token: default stringa vuota, l'endpoint di
        # registrazione ne genera uno quando assente.
        ("delegation_webhook_token", ""),
    ],
)
def test_catalog_settings_semantically_optional_accept_empty_value(key, empty_value):
    """Pin di regressione: queste tre voci del CATALOG sono dichiarate
    required=False (e non più il default required=True) perché svuotarle è
    un'operazione legittima e già prevista dal comportamento esistente
    dell'app (spegnere il sync, non registrare i webhook)."""
    setting = sc.get_def(key)
    assert setting is not None, f"'{key}' non è più nel CATALOG?"
    assert setting.required is False, f"'{key}' deve essere required=False"
    sr.validate_value(setting, empty_value, _FakeRegistry())  # non deve sollevare
