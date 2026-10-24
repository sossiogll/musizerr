from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core.auth_manager import AuthError
from musizeer_sdk.auth import AuthFlow
from core.interfaces import metadata_capabilities
from core.settings_resolver import (
    PROVIDER_CATEGORY_GETTERS,
    compute_provider_usable,
    find_provider_by_name,
)
from routes.deps import (
    AdminUserDep,
    AuthManagerDep,
    ConfigDep,
    SharedStoreDep,
    CurrentUserDep,
    MetadataPriorityDep,
    RegistryDep,
    SessionDep,
)
from services.auth import auth_manager_for
from services.credentials import own_store, shared_store, user_store
from services.gateway_sync import GatewaySync
from services.linked_accounts import credential_fields, personal_state, shared_state
from services.settings import SettingsService, SettingsWriteError

router = APIRouter()


@router.get("/config/download-states")
def get_download_states():
    """Vocabolario canonico degli stati di download (key/label/tono) per la UI."""
    from core.download_states import public_states

    return {"states": public_states()}


@router.post("/config/verify-full-sync")
def verify_full_sync_start(admin: AdminUserDep):  # noqa: ARG001
    """Avvia il canary probe che verifica il full-sync mode (async)."""
    from workers.maintenance import verify_full_sync

    task = verify_full_sync.delay()
    return {"task_id": task.id}


@router.get("/config/verify-full-sync/{task_id}")
def verify_full_sync_status(task_id: str, admin: AdminUserDep):  # noqa: ARG001
    """Stato/risultato del probe full-sync. Admin-only come il suo avvio: il
    risultato riporta dettagli dell'infrastruttura."""
    from workers.celery_app import celery

    res = celery.AsyncResult(task_id)
    if not res.ready():
        return {"ready": False, "result": None}
    if res.successful():
        return {"ready": True, "result": res.result}
    return {"ready": True, "result": {"status": "error", "detail": str(res.result)}}


class SettingsPatch(BaseModel):
    model_config = {"extra": "allow"}


def _raise_for_write_error(exc: SettingsWriteError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/settings/schema")
def get_settings_schema(config: ConfigDep, registry: RegistryDep, session: SessionDep, user: CurrentUserDep):
    """
    Schema unificato (CATALOG core + impostazioni di ogni provider registrato),
    filtrato dalla policy di lettura dichiarata per il ruolo di ``user``. Una
    voce system-scoped non compare affatto per un non-admin. Unico endpoint
    di schema rimasto — ``GET /config/schema`` (vocabolario legacy) è stato
    rimosso insieme al resto del percorso legacy.

    ``taxonomy`` porta i metadati di area/gruppo (label, descrizione, ordine
    di visualizzazione) del CATALOG core — non è filtrata per ruolo (sono
    metadati di struttura, non valori), e non elenca i gruppi di provider
    (la loro label viene da GET /providers).

    Ogni voce porta anche ``available`` e
    ``unavailable_reason`` — il verdetto dei requisiti dichiarati
    (``SettingDef.requires``) contro lo stato ATTUALE, mai contro un nome di
    provider hardcoded (il motivo è costruito da categoria/label dichiarate,
    vedi ``core.settings_resolver.setting_availability``).
    """
    from core.settings_catalog import public_taxonomy

    service = SettingsService(session, config, registry)
    availability = service.evaluate_availability(user)

    settings = []
    for s in service.readable_schema(user):
        public = s.to_public()
        available, reason = availability.get(s.key, (True, None))
        public["available"] = available
        public["unavailable_reason"] = reason
        settings.append(public)

    return {
        "settings": settings,
        "taxonomy": public_taxonomy(),
    }


@router.get("/settings/values")
def get_settings_values(config: ConfigDep, registry: RegistryDep, session: SessionDep, user: CurrentUserDep):
    """
    Valore effettivo di ogni impostazione leggibile da ``user`` (override
    personale -> valore di sistema salvato -> default dichiarato). Una chiave
    non leggibile è omessa, non svuotata.
    """
    service = SettingsService(session, config, registry)
    return service.effective_values(user)


@router.patch("/settings")
def patch_settings(
    body: SettingsPatch, config: ConfigDep, registry: RegistryDep, session: SessionDep, user: CurrentUserDep,
    adopt_into: Optional[str] = Query(None),
):
    """
    Applica un batch di modifiche come unità atomica: chiave sconosciuta,
    vietata per il ruolo, non coercibile o non valida rifiuta l'INTERA
    richiesta (nulla viene scritto). Instrada ogni chiave allo storage giusto
    guidato dalla sua dichiarazione (namespace di provider / scope), mai dal
    nome della chiave.

    ``adopt_into`` (id di un amministratore): chi riceve i dati del proprietario
    dell'istanza se la scrittura accende l'autenticazione (di default il più
    anziano che possa accedere); ignorato in ogni altro caso.
    """
    service = SettingsService(session, config, registry)
    updates = body.model_dump(exclude_none=True)
    try:
        values = service.write_values(updates, user, adopt_into=adopt_into)
    except SettingsWriteError as exc:
        _raise_for_write_error(exc)
    if service.folder_keys_among(updates):
        # The gateway learns the working folder (and the folders it grants) now, not at its next check (ADR-0008).
        GatewaySync().push(session)
    return values


@router.get("/providers/public")
def list_public_providers(registry: RegistryDep, priority: MetadataPriorityDep):
    """Etichette dei provider, ordine di priorità dei metadati e capability
    opzionali che ciascun provider di metadati dichiara (es. per mostrare
    il nome del provider invece dell'id, o l'azione che solo alcuni sanno fare), per
    chiunque: nessuna credenziale, configurazione o stato. La priorità è
    l'ordine EFFETTIVO di chi guarda (``services/provider_order``: la propria
    lista se ne ha una, altrimenti quella di sistema, ristretta ai provider
    registrati, con in coda quelli che la lista non nomina, per ordine di
    installazione): ``metadata_priority`` e ``metadata`` li elencano tutti,
    una volta sola; ``metadata_capabilities`` le chiavi di
    ``musizeer_sdk.capabilities`` che ognuno dichiara."""
    labels: dict[str, str] = {}
    for getter_name in PROVIDER_CATEGORY_GETTERS:
        for provider in getattr(registry, getter_name)():
            labels.setdefault(provider.name, provider.label)
    metadata = registry.all_metadata()
    return {
        "providers": [{"id": name, "label": label} for name, label in labels.items()],
        "metadata_priority": priority,
        "metadata": list(priority),
        "metadata_capabilities": {p.name: metadata_capabilities(p) for p in metadata},
    }


@router.get("/providers")
def list_providers(registry: RegistryDep, config: ConfigDep, session: SessionDep, user: CurrentUserDep):
    """
    Lista tutti i provider con schema di configurazione e stato auth, UNA
    voce per OGNI categoria che il registry conosce (``PROVIDER_CATEGORY_GETTERS``
    in core/settings_resolver.py — la stessa lista che ``merged_schema`` usa
    per costruire lo schema unificato). Derivata genericamente, mai scritta a
    mano: ogni categoria con impostazioni proprie ha anche le sue etichette qui.

    Metadata providers show per-user credential scope (with system fallback)
    for AUTH purposes. Provider *config* (operational settings, e.g. a shared
    server URL) is always resolved at system scope through SettingsService,
    for every category including metadata — it is deployment-wide, and must
    agree with what PATCH /settings writes to (namespaced keys
    "provider.<name>.<short_key>", see core/settings_resolver.py).
    """
    viewer_auth_manager = auth_manager_for(user_store(session, user.id))
    own = own_store(session, user.id)
    shared = shared_store(session)
    settings_service = SettingsService(session, config, registry)
    is_admin = user.role == "admin"

    # Un provider REGISTRATO in più categorie (es. un provider sia metadata sia
    # sync) va identificato per OGGETTO — mai per `name`, che nel registry può
    # collidere fra due CLASSI DISTINTE registrate ciascuna sotto una
    # categoria diversa (es. due provider omonimi, uno metadata e uno
    # download, con schemi diversi). Stesso pattern di dedup per identità già usato da
    # ``tests/test_settings_schema_conformance.py::_all_providers`` (chiave
    # ``id(p)``, mai il nome). Calcolato una volta scorrendo OGNI categoria
    # dichiarata dal registry, mai una lista di categorie scritta a mano.
    categories_by_identity: dict[int, list[str]] = {}
    for getter_name in PROVIDER_CATEGORY_GETTERS:
        category = getter_name.removeprefix("all_")
        for p in getattr(registry, getter_name)():
            categories_by_identity.setdefault(id(p), []).append(category)

    result: dict[str, list[dict]] = {}
    for getter_name in PROVIDER_CATEGORY_GETTERS:
        category = getter_name.removeprefix("all_")
        result[category] = [
            _provider_info(
                p, viewer_auth_manager, own, shared, is_admin, settings_service,
                also_registered_in=[c for c in categories_by_identity[id(p)] if c != category],
            )
            for p in getattr(registry, getter_name)()
        ]

    return result


@router.delete("/providers/{name}/accounts/{account_name}")
def delete_provider_account(
    name: str,
    account_name: str,
    registry: RegistryDep,
    store: SharedStoreDep,
    admin: AdminUserDep,  # noqa: ARG001
):
    # Lookup generico su TUTTE le categorie (core.settings_resolver
    # .find_provider_by_name), condiviso con routes/auth.py::_get_provider —
    # mai un secondo elenco scritto a mano di categorie qui.
    provider = find_provider_by_name(registry, name)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    if provider.auth.flow != AuthFlow.EXTERNAL_UTILITY or not provider.auth.external_utility:
        raise HTTPException(status_code=400, detail="Provider does not use external utility auth")
    removed = store.delete_external_utility_account(
        provider.name, provider.auth.external_utility.credential_type, account_name,
    )
    if not removed:
        raise HTTPException(status_code=404, detail=f"Account '{account_name}' not found")
    return {"ok": True}


def _provider_info(
    provider, auth_manager, own, store, is_admin: bool, settings_service: SettingsService,
    *, also_registered_in: list[str] | None = None,
) -> dict:
    """``auth_manager`` authenticates as the viewer (the routing of the
    provider's account model); ``own`` is the viewer's own store and ``store``
    the instance's shared one (its app client, API key, external accounts)."""
    authenticated = False
    try:
        if provider.auth.flow != AuthFlow.NONE:
            auth_manager.authenticate(provider)
            authenticated = True
        else:
            authenticated = True
    except AuthError:
        authenticated = False
    except Exception:
        authenticated = False
    finally:
        # Se un tentativo di auth ha causato un errore di scrittura (es. token refresh
        # su DB readonly), la sessione resta in stato PendingRollback.
        # Rollback preventivo per ripristinare la sessione e permettere le letture successive.
        try:
            store.rollback()
        except Exception:
            pass


    # `authenticated` è vero a vuoto per flow=NONE (nessun login da tentare),
    # quindi da solo non dice se il provider è davvero utilizzabile — un
    # provider senza login ma con config incompleta (es. senza la chiave API
    # del suo servizio) risulterebbe comunque "connesso". `usable` combina il segnale di
    # login con la stessa domanda di completezza già esposta in
    # `config_valid` (e, per i provider di identificazione, il loro stesso
    # check di disponibilità) per dare un dot di stato veritiero. Additivo:
    # `authenticated` resta invariato per non rompere nulla che già lo legge.
    # `compute_provider_usable` è l'UNICA definizione di "usable":
    # la valutazione dei requisiti delle impostazioni la riusa identica
    # (services.settings.provider_is_usable), mai una seconda definizione.
    config_valid = settings_service.provider_config_is_valid(provider)
    usable = compute_provider_usable(provider, authenticated=authenticated, config_valid=config_valid)

    extra: dict = {}
    if provider.auth.flow == AuthFlow.OAUTH2:
        cid = store.get_oauth2_client_id(provider.name)
        if cid:
            extra["client_id"] = cid
    elif provider.auth.flow == AuthFlow.CREDENTIALS:
        extra["credential_fields"] = credential_fields(provider)
    elif provider.auth.flow == AuthFlow.EXTERNAL_UTILITY:
        cfg = provider.auth.external_utility
        if cfg:
            accounts = store.get_external_utility_accounts(provider.name, cfg.credential_type)
            display_fields = cfg.account_display_fields or []
            extra["accounts"] = [
                {"name": name, **{f.key: data.get(f.key, "") for f in display_fields}}
                for name, data in accounts.items()
            ]
            extra["account_display_fields"] = [
                {"key": f.key, "label": f.label} for f in display_fields
            ]
            if cfg.borrow_oauth_client_from:
                extra["borrowed_oauth_client_from"] = cfg.borrow_oauth_client_from
                extra["borrowed_oauth_client_ok"] = bool(
                    store.get_oauth2_client_id(cfg.borrow_oauth_client_from)
                )
            if cfg.library_package:
                extra["library_package"] = cfg.library_package
            if cfg.library_url:
                extra["library_url"] = cfg.library_url
            if cfg.disclaimer:
                extra["disclaimer"] = cfg.disclaimer
            if cfg.setup_command:
                extra["setup_command"] = cfg.setup_command
            if cfg.setup_instructions:
                extra["setup_instructions"] = cfg.setup_instructions
    library_available = provider.is_library_available()
    if library_available is not None:
        extra["library_available"] = library_available
    if "metadata" in provider.roles:
        # Optional capabilities it declares (musizeer_sdk.capabilities), and those the
        # viewer's OWN connection was not granted the permission for (made
        # before the provider asked for it): the UI says "reconnect".
        extra["capabilities"] = metadata_capabilities(provider)

    return {
        "name": provider.name,
        "label": provider.label,
        "auth_flow": provider.auth.flow.value,
        "authenticated": authenticated,
        "usable": usable,
        # Whose account the provider is used with (declared by the provider)
        # and what is connected: the viewer's own, and the instance's shared
        # one (who it is, only for admins). Null where the model has none.
        "account_model": provider.auth.account_model.value,
        "personal": personal_state(provider, own),
        "shared": shared_state(provider, store, is_admin=is_admin),
        **extra,
        # La config (es. URL server condiviso) è roba di sistema, admin-only.
        # Un non-admin che collega il proprio account non ne ha bisogno e non
        # deve vederla. Risolta SEMPRE a scope di sistema (mai il fallback
        # per-utente di `store`), cosi' concorda con PATCH /settings (chiavi
        # namespaced "provider.<nome>.<short_key>").
        "config": settings_service.provider_config(provider.name) if is_admin else {},
        # Risponde alla domanda di COMPLETEZZA che validate_config pone davvero — "questo
        # provider, con la config attualmente salvata, è utilizzabile?" — mai
        # usato come gate di scrittura (vedi la nota in
        # services/settings.write_values sul perché sarebbe un errore).
        # Non svela alcun valore di config a un non-admin, solo un booleano.
        "config_valid": config_valid,
        # Altre categorie in cui QUESTO STESSO oggetto provider (identità, non
        # nome — vedi list_providers) è registrato. Vuoto di default: un provider a ruolo singolo (la
        # stragrande maggioranza) non lo popola mai. Il frontend lo usa per
        # decidere se due entry con lo stesso nome sono davvero lo stesso
        # provider (dedup dell'editor) o una collisione di nomi fra classi
        # distinte (ciascuna con il proprio editor pieno).
        "also_registered_in": also_registered_in or [],
    }
