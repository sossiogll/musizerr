"""
Tassonomia dichiarativa delle impostazioni (area / gruppo).

Copre l'invariante che questo passaggio introduce: OGNI voce del CATALOG
dichiara un'area valida, OGNI gruppo appartiene a esattamente un'area (mai
un gruppo "general" che finisce per ospitare impostazioni di aree diverse),
area e gruppo hanno una label dichiarata e un ordine deterministico, e la
mappatura area/gruppo → chiavi è ESATTAMENTE quella specificata (conteggi
per area/gruppo compresi, così un edit futuro che sposta una voce nel gruppo
sbagliato fallisce rumorosamente).

Regola d'oro rispettata: le impostazioni di provider sono verificate
iterando genericamente su un ``ProviderRegistry`` con un provider finto per categoria — nessuna asserzione
nomina un provider concreto.
"""

from __future__ import annotations

from core import download_policy
from core import settings_catalog as sc
from core import settings_resolver as sr
from tests.settings_support import registry_of_every_category


# ---------------------------------------------------------------------------
# Ogni voce del CATALOG dichiara un'area valida
# ---------------------------------------------------------------------------


def test_every_catalog_entry_declares_a_valid_area():
    for s in sc.CATALOG:
        assert s.area, f"'{s.key}' non dichiara un'area"
        assert s.area in sc.AREA_ORDER, f"'{s.key}' dichiara un'area sconosciuta: '{s.area}'"


def test_every_catalog_entry_area_matches_its_group_area():
    # L'invariante vero e proprio: l'area di ogni voce deve coincidere con
    # l'area dichiarata per il suo gruppo (GROUP_AREAS) — nessuna voce può
    # "disallinearsi" dal proprio gruppo.
    for s in sc.CATALOG:
        assert s.group in sc.GROUP_AREAS, f"'{s.key}' usa un gruppo non tassonomizzato: '{s.group}'"
        assert s.area == sc.GROUP_AREAS[s.group], (
            f"'{s.key}': area '{s.area}' non combacia con l'area dichiarata "
            f"per il gruppo '{s.group}' ('{sc.GROUP_AREAS[s.group]}')"
        )


# ---------------------------------------------------------------------------
# Ogni gruppo appartiene a esattamente un'area — l'invariante anti-"general"
# ---------------------------------------------------------------------------


def test_every_group_maps_to_exactly_one_area():
    group_to_areas: dict[str, set[str]] = {}
    for s in sc.CATALOG:
        group_to_areas.setdefault(s.group, set()).add(s.area)

    for group, areas in group_to_areas.items():
        assert len(areas) == 1, (
            f"il gruppo '{group}' compare con più di un'area nel CATALOG: {areas} "
            "— esattamente il problema che questa tassonomia deve impedire"
        )


def test_group_metadata_declares_exactly_one_area_each():
    for g in sc.GROUPS:
        assert g.area in sc.AREA_ORDER
    # GROUP_AREAS e GROUPS devono concordare: stessa fonte di verità vista da
    # due strutture diverse (dict veloce vs tuple ordinata per la UI).
    assert sc.GROUP_AREAS == {g.key: g.area for g in sc.GROUPS}


def test_no_group_used_in_catalog_is_missing_from_group_metadata():
    catalog_groups = {s.group for s in sc.CATALOG}
    declared_groups = {g.key for g in sc.GROUPS}
    assert catalog_groups <= declared_groups, (
        f"gruppi usati nel CATALOG ma senza metadati dichiarati: {catalog_groups - declared_groups}"
    )


# ---------------------------------------------------------------------------
# Label e ordine deterministico
# ---------------------------------------------------------------------------


def test_every_area_has_a_label():
    for area in sc.AREA_ORDER:
        assert sc.AREA_LABELS.get(area), f"area '{area}' senza label"


def test_every_group_has_a_label_and_description():
    for g in sc.GROUPS:
        assert g.label, f"gruppo '{g.key}' senza label"
        assert g.description, f"gruppo '{g.key}' senza descrizione"


def test_area_order_is_deterministic_and_matches_spec():
    # Ordine di visualizzazione richiesto esplicitamente: account, providers,
    # library, system.
    assert sc.AREA_ORDER == (
        sc.AREA_ACCOUNT, sc.AREA_PROVIDERS, sc.AREA_LIBRARY, sc.AREA_SYSTEM,
    )


def test_public_taxonomy_is_stable_and_ordered():
    t1 = sc.public_taxonomy()
    t2 = sc.public_taxonomy()
    assert t1 == t2  # nessuna dipendenza da ordinamento di dict/set non deterministico

    area_keys = [a["key"] for a in t1["areas"]]
    assert area_keys == list(sc.AREA_ORDER)

    # I gruppi sono ordinati per area (nell'ordine di AREA_ORDER) e, dentro
    # ogni area, nell'ordine dichiarato in GROUPS.
    group_areas_in_order = [g["area"] for g in t1["groups"]]
    seen = []
    for a in group_areas_in_order:
        if not seen or seen[-1] != a:
            seen.append(a)
    # Le aree, nell'ordine in cui compaiono per la prima volta fra i gruppi,
    # devono rispettare AREA_ORDER (a meno di aree senza alcun gruppo).
    assert seen == [a for a in sc.AREA_ORDER if a in group_areas_in_order]


def test_public_taxonomy_groups_carry_label_description_and_area():
    for g in sc.public_taxonomy()["groups"]:
        assert {"key", "label", "description", "area"} <= set(g)
        assert g["area"] in sc.AREA_ORDER


# ---------------------------------------------------------------------------
# La mappatura esatta richiesta (Task 2) — conteggi per area e per gruppo
# ---------------------------------------------------------------------------


def _keys_for_group(group: str) -> set[str]:
    return {s.key for s in sc.CATALOG if s.group == group}


def _keys_for_area(area: str) -> set[str]:
    return {s.key for s in sc.CATALOG if s.area == area}


def test_account_area_mapping_exact():
    assert _keys_for_area(sc.AREA_ACCOUNT) == {
        "language", "theme", "background", "metadata_provider_priority", "unlike_on_origin_providers",
    }
    assert _keys_for_group(sc.GROUP_ACCOUNT) == _keys_for_area(sc.AREA_ACCOUNT)


def test_providers_area_mapping_exact_for_core_catalog():
    # Nel CATALOG (senza le voci di provider, aggiunte solo da merged_schema)
    # l'area "providers" contiene solo il gruppo provider_selection.
    assert _keys_for_area(sc.AREA_PROVIDERS) == {
        "storage_provider", "sync_provider", "download_provider_order",
    }
    assert _keys_for_group(sc.GROUP_PROVIDER_SELECTION) == _keys_for_area(sc.AREA_PROVIDERS)


def test_library_area_mapping_exact():
    expected_by_group = {
        sc.GROUP_FILES: {
            "workspace_dir", "held_upload_max_age_hours", "save_cover", "track_format",
            "stream_start_timeout", "storage_stream_cooldown_seconds",
        },
        sc.GROUP_DOWNLOADS: {
            "max_concurrent_downloads", "max_retries", "retry_delay_seconds",
            "http_timeout", "download_timeout", "connect_timeout",
            "download_soft_time_limit_seconds", "download_hard_time_limit_seconds",
            "provider_slot_wait_seconds",
            "retry_max_delay_seconds", "provider_cooldown_errors", "provider_cooldown_seconds",
            "transient_retry_after_hours", "transient_retry_max_rounds", "transient_retry_batch_size",
        },
        sc.GROUP_DOWNLOAD_REQUESTS: {
            *(download_policy.mode_key(kind) for kind in download_policy.KINDS),
            *(download_policy.threshold_key(kind) for kind in download_policy.KINDS),
            "download_request_quota_tracks", "download_request_quota_days",
        },
        sc.GROUP_INTEGRITY: {
            "verify_integrity_enabled", "verify_duration_tolerance_seconds", "verify_after_upload",
        },
        sc.GROUP_IDENTIFICATION: {
            "identification_enabled", "identification_confidence_threshold", "anti_mislabel_enabled",
            "dedup_enabled", "dedup_prefer_lossless",
        },
        sc.GROUP_LIBRARY_SYNC: {
            "full_sync_probe_timeout_seconds", "full_sync_probe_interval_seconds",
        },
        sc.GROUP_PLAYLISTS: {"playlist_max_tracks"},
    }
    for group, expected_keys in expected_by_group.items():
        assert _keys_for_group(group) == expected_keys, f"gruppo '{group}' non combacia"

    all_expected = set().union(*expected_by_group.values())
    assert _keys_for_area(sc.AREA_LIBRARY) == all_expected
    assert len(all_expected) == 44


def _routine_setting_keys() -> set[str]:
    from core.routines import ROUTINES, enabled_setting_key, interval_setting_key

    keys: set[str] = set()
    for routine in ROUTINES:
        keys.add(enabled_setting_key(routine.key))
        keys.add(interval_setting_key(routine.key))
    return keys


def test_system_area_mapping_exact():
    expected_by_group = {
        sc.GROUP_AUTH: {
            "auth_enabled", "registration_open", "provider_login_new_accounts", "token_expiry_buffer",
        },
        sc.GROUP_SEARCH: {
            "search_default_limit", "search_provider_budget_seconds", "enrichment_budget_seconds",
            "search_stream_enrich_workers", "sync_availability_cache_ttl_seconds",
        },
        sc.GROUP_RECONCILIATION: {
            "delegation_webhook_enabled", "delegation_poll_interval_seconds",
            "like_reconcile_interval_seconds", "like_reconcile_max_age_hours",
            "delegation_max_age_hours", "delegation_max_wait_hours",
            "delegation_webhook_token", "public_base_url",
        },
        sc.GROUP_ROUTINES: _routine_setting_keys(),
    }
    for group, expected_keys in expected_by_group.items():
        assert _keys_for_group(group) == expected_keys, f"gruppo '{group}' non combacia"

    all_expected = set().union(*expected_by_group.values())
    assert _keys_for_area(sc.AREA_SYSTEM) == all_expected
    assert len(all_expected) == 43


def test_routine_settings_are_hidden_from_the_generic_settings_page():
    # Proprietà della futura pagina "System routines" — la pagina
    # impostazioni generica deve saltarle (vedi SettingDef.hidden).
    for key in _routine_setting_keys():
        assert sc.get_def(key).hidden is True


def test_catalog_area_counts_are_exactly_5_3_44_43():
    counts = {area: len(_keys_for_area(area)) for area in sc.AREA_ORDER}
    assert counts == {
        sc.AREA_ACCOUNT: 5,
        sc.AREA_PROVIDERS: 3,
        sc.AREA_LIBRARY: 44,
        sc.AREA_SYSTEM: 43,
    }
    assert sum(counts.values()) == len(sc.CATALOG) == 95


# ---------------------------------------------------------------------------
# Impostazioni di provider: sempre area "providers", sempre sotto il proprio
# gruppo — mai qualcosa che il provider deve ricordarsi di dichiarare.
# Iterazione GENERICA sul registry reale: nessun nome di provider nominato.
# ---------------------------------------------------------------------------


def test_every_provider_declared_setting_lands_in_providers_area_under_its_own_group():
    registry = registry_of_every_category()
    merged = sr.merged_schema(registry)

    core_keys = {s.key for s in sc.CATALOG}
    provider_settings = [s for s in merged if s.key not in core_keys]

    assert provider_settings, (
        "nessuna impostazione di provider trovata nello schema unificato: il "
        "registry non ha provider con config_schema() non vuoto?"
    )

    for setting in provider_settings:
        assert setting.area == sc.AREA_PROVIDERS, (
            f"'{setting.key}': le impostazioni di provider devono vivere "
            f"nell'area '{sc.AREA_PROVIDERS}', trovata '{setting.area}'"
        )
        # La chiave è namespaced come provider.<nome>.<short_key>, e il gruppo
        # è già il nome del provider stesso (l'host lo imposta al nome del provider in
        # settings_resolver.provider_setting_def) — mai un valore che il provider
        # ha dovuto ricordarsi di allineare all'area.
        provider_name, _short_key = sr.split_provider_key(setting.key)
        assert provider_name is not None, f"'{setting.key}' non è una chiave namespaced di provider"
        assert setting.group == provider_name, (
            f"'{setting.key}': gruppo atteso '{provider_name}' (nome del provider), trovato '{setting.group}'"
        )


def test_every_list_and_provider_ref_setting_declares_a_provider_category():
    # Fase 5a fix 1: `provider_category` esisteva già come campo di SettingDef
    # ma era dichiarato SOLO dai `provider_ref` — i due `list` del CATALOG
    # (metadata_provider_priority, download_provider_order) non lo
    # dichiaravano, costringendo il frontend a tenere una propria mappa
    # chiave->categoria (esattamente la conoscenza che questo campo esiste
    # per evitare di dover duplicare). Verificato sull'intero schema
    # unificato (CATALOG + provider), cosi' una futura impostazione di
    # provider di tipo "list"/"provider_ref" senza categoria fa fallire
    # rumorosamente questo test invece di silenziosamente ricreare il
    # problema lato frontend.
    registry = registry_of_every_category()
    merged = sr.merged_schema(registry)
    for setting in merged:
        if setting.type in ("list", "provider_ref"):
            assert setting.provider_category, (
                f"'{setting.key}' è di tipo '{setting.type}' ma non dichiara "
                "provider_category"
            )


def test_provider_settings_never_collide_with_a_core_catalog_group():
    # Un provider potrebbe (per sfortuna) chiamarsi come un gruppo del CATALOG
    # core: se succedesse, l'area verrebbe comunque forzata correttamente da
    # merged_schema (verificato sopra) — qui si controlla solo che il registry
    # reale oggi non abbia questa collisione, il caso comune.
    registry = registry_of_every_category()
    provider_names = {
        p.name
        for getter in (
            registry.all_metadata, registry.all_download, registry.all_lyrics,
            registry.all_storage, registry.all_sync, registry.all_identification,
        )
        for p in getter()
    }
    assert provider_names.isdisjoint(sc.GROUP_AREAS.keys())
