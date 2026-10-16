"""
Fase 5c (backend): requisiti di DISPONIBILITÀ di una SettingDef
(``core.settings_catalog.Requirement``) e la loro valutazione pura in
``core/settings_resolver.py`` — ``compute_provider_usable``,
``evaluate_requirement``, ``setting_availability``.

Diverso da ``visible_when`` (già coperto altrove): un requisito decide se
un'impostazione ha senso di essere ACCESA nello stato attuale, non se va
mostrata in UI.

Regola d'oro rispettata: nessun nome di provider concreto — le categorie
usate nelle asserzioni sono stringhe dichiarative ("identification", "sync"),
mai un provider reale.
"""

from __future__ import annotations

import pytest

from core import settings_catalog as sc
from core import settings_resolver as sr
from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow

from tests.conftest import FakeIdentificationProvider, FakeMetadataProvider
from tests.loopback import as_proxy


# ---------------------------------------------------------------------------
# compute_provider_usable — unica definizione di "usable"
# ---------------------------------------------------------------------------


def test_compute_provider_usable_true_when_authenticated_and_config_valid():
    provider = as_proxy(FakeMetadataProvider("m"))
    assert sr.compute_provider_usable(provider, authenticated=True, config_valid=True) is True


@pytest.mark.parametrize("authenticated,config_valid", [(False, True), (True, False), (False, False)])
def test_compute_provider_usable_false_unless_both_authenticated_and_config_valid(authenticated, config_valid):
    provider = as_proxy(FakeMetadataProvider("m"))
    assert sr.compute_provider_usable(provider, authenticated=authenticated, config_valid=config_valid) is False


def test_compute_provider_usable_identification_also_requires_is_available():
    available_provider = as_proxy(FakeIdentificationProvider("ident-ok", available=True))
    unavailable_provider = as_proxy(FakeIdentificationProvider("ident-ko", available=False))

    assert sr.compute_provider_usable(available_provider, authenticated=True, config_valid=True) is True
    # Autenticato e configurato correttamente, ma is_available() è False
    # (es. libreria/dipendenza esterna mancante): non usable.
    assert sr.compute_provider_usable(unavailable_provider, authenticated=True, config_valid=True) is False


# ---------------------------------------------------------------------------
# evaluate_requirement / setting_availability — valutazione pura
# ---------------------------------------------------------------------------


def _category_usable(flags: dict[str, bool]):
    def _fn(category: str) -> bool:
        return flags.get(category, False)
    return _fn


def test_provider_category_requirement_satisfied_and_unsatisfied():
    req = sc.requires_provider_category("identification")

    ok, reason = sr.evaluate_requirement(
        req, effective_values={}, schema_by_key={}, category_usable=_category_usable({"identification": True}),
    )
    assert ok is True
    assert reason is None

    ok, reason = sr.evaluate_requirement(
        req, effective_values={}, schema_by_key={}, category_usable=_category_usable({"identification": False}),
    )
    assert ok is False
    # Il motivo nomina la CATEGORIA (dato dichiarato), mai un nome di provider.
    assert "identification" in reason


def test_setting_set_requirement_satisfied_and_unsatisfied_uses_declared_label():
    other = sc.SettingDef("sync_provider", "provider_ref", None, "g", "Sync provider", provider_category="sync")
    req = sc.requires_setting("sync_provider")
    schema_by_key = {"sync_provider": other}

    ok, reason = sr.evaluate_requirement(
        req, effective_values={"sync_provider": "x"}, schema_by_key=schema_by_key,
        category_usable=_category_usable({}),
    )
    assert ok is True
    assert reason is None

    for empty in (None, "", []):
        ok, reason = sr.evaluate_requirement(
            req, effective_values={"sync_provider": empty}, schema_by_key=schema_by_key,
            category_usable=_category_usable({}),
        )
        assert ok is False
        # Il motivo è costruito dalla LABEL dichiarata, non dalla chiave grezza.
        assert reason == "requires 'Sync provider' to be set"


def test_setting_set_requirement_falls_back_to_key_when_other_setting_unknown():
    req = sc.requires_setting("ghost_setting")
    ok, reason = sr.evaluate_requirement(
        req, effective_values={}, schema_by_key={}, category_usable=_category_usable({}),
    )
    assert ok is False
    assert "ghost_setting" in reason


def test_setting_availability_combines_multiple_unmet_requirements():
    setting = sc.SettingDef(
        "k", "bool", False, "g", "L",
        requires=(sc.requires_provider_category("identification"), sc.requires_setting("sync_provider")),
    )
    other = sc.SettingDef("sync_provider", "provider_ref", None, "g", "Sync provider", provider_category="sync")
    schema_by_key = {"sync_provider": other}

    available, reason = sr.setting_availability(
        setting,
        effective_values={"sync_provider": None},
        schema_by_key=schema_by_key,
        category_usable=_category_usable({"identification": False}),
    )
    assert available is False
    assert "identification" in reason
    assert "Sync provider" in reason


def test_setting_availability_true_with_no_requirements():
    setting = sc.SettingDef("k", "bool", False, "g", "L")
    available, reason = sr.setting_availability(
        setting, effective_values={}, schema_by_key={}, category_usable=_category_usable({}),
    )
    assert available is True
    assert reason is None


# ---------------------------------------------------------------------------
# Il CATALOG dichiara esattamente i requisiti verificati contro il codice
# reale (vedi i commenti in core/settings_catalog.py accanto a ciascuna voce).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["identification_enabled", "identification_confidence_threshold"],
)
def test_identification_group_requires_usable_identification_provider(key):
    setting = sc.get_def(key)
    assert setting.requires == (sc.requires_provider_category("identification"),)


def test_anti_mislabel_additionally_requires_identification_enabled():
    # Revisione post-deadlock (vedi core/settings_catalog.py e
    # AcoustIDProvider.is_available()): il call site generico in
    # app/workers/tasks.py ora richiede esplicitamente
    # "config.anti_mislabel_enabled and config.identification_enabled" prima
    # di consultare un provider di identificazione — la dipendenza, prima
    # falsa (e correttamente rifiutata), e' stata resa vera aggiungendo
    # quel controllo, proprio per poter togliere identification_enabled da
    # dentro is_available() senza perdere il comportamento.
    setting = sc.get_def("anti_mislabel_enabled")
    assert setting.requires == (
        sc.requires_provider_category("identification"),
        sc.requires_setting("identification_enabled"),
    )


@pytest.mark.parametrize(
    "key",
    [
        "full_sync_probe_timeout_seconds",
        "full_sync_probe_interval_seconds",
        "like_reconcile_interval_seconds",
        "like_reconcile_max_age_hours",
    ],
)
def test_sync_dependent_settings_require_sync_provider_setting(key):
    setting = sc.get_def(key)
    assert setting.requires == (sc.requires_setting("sync_provider"),)


@pytest.mark.parametrize("key", ["dedup_enabled", "dedup_prefer_lossless"])
def test_dedup_settings_declare_no_requirement(key):
    # Oggi la deduplicazione risolve conflitti riportati da un sistema di
    # download esterno confrontando la qualità dichiarata (vedi
    # _resolve_conflicts in app/workers/maintenance.py) — non usa
    # l'identificazione. Dichiarare un requisito qui sarebbe falso.
    setting = sc.get_def(key)
    assert setting.requires == ()


@pytest.mark.parametrize("key", ["routine.sync_saved_tracks.interval_minutes", "sync_availability_cache_ttl_seconds"])
def test_settings_refused_despite_being_listed_declare_no_requirement(key):
    # Rifiutati deliberatamente (vedi commenti nel CATALOG accanto a queste
    # due voci): il codice reale non li rende inerti senza sync_provider.
    # - routine.sync_saved_tracks.interval_minutes pianifica
    #   sync_all_users_saved_tracks, che sincronizza le tracce salvate dai
    #   METADATA provider, mai il SyncProvider referenziato da "sync_provider"
    #   (app/core/routines.py, app/workers/scheduler.py).
    # - sync_availability_cache_ttl_seconds è consultato per OGNI sync
    #   provider registrato (registry.all_sync()), non solo quello attivo
    #   (app/services/enrichment.py).
    setting = sc.get_def(key)
    assert setting.requires == ()
