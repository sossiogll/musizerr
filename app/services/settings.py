"""
Servizio applicativo che siede fra le route HTTP e l'UNICO store delle
impostazioni, la tabella ``settings`` (``db/settings_store.py``,
``SettingsRepository``). ``ConfigManager`` (``services/config_manager.py``) è
una facade sopra la stessa tabella e resta l'unico canale per le
impostazioni app-level generiche (quelle lette anche da worker/hot path),
mentre questo modulo instrada direttamente sul repository le impostazioni di
provider e gli override per-utente, che ``ConfigManager`` non modella
(nessun concetto di "provider" o "utente corrente" al suo livello).

Il motore di risoluzione (``core/settings_resolver.py``) fornisce lo schema
unificato e le policy di lettura/scrittura; questo modulo aggiunge la logica
di ORCHESTRAZIONE — dove va letto/scritto ciascun valore.

Provider-agnostico: nessun nome di provider concreto compare qui. Il routing
di persistenza è guidato ESCLUSIVAMENTE dal namespace della chiave
(``provider.<nome>.<short_key>`` vs chiave app-level) e dallo ``scope``
dichiarato in ``SettingDef`` — mai da un confronto su un nome di provider o di
chiave specifico.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from musizerr_sdk.auth import AuthFlow
from core.auth_manager import AuthError, AuthManager
from services.config_manager import ConfigManager
from core.interfaces import ProviderRegistry
from core.settings_catalog import CATALOG, SettingDef, SettingRefused, get_def
from core.settings_resolver import (
    SettingError,
    can_read,
    can_write,
    coerce_value,
    compute_provider_usable,
    is_empty_value,
    merged_schema,
    provider_config_from_system_settings,
    setting_availability,
    split_provider_key,
    validate_value,
)
from db import user_service
from db.models import User
from db.settings_store import SettingsRepository
from services import auth_transition, provider_order
from services.auth import auth_manager_for
from services.credentials import shared_store, user_store

_ADMIN_ROLE = "admin"

# Sentinel distinguishing "no row for this user" from any real stored value
# (including one that happens to equal the declared default) for scope='user'
# settings. Never leaks past _read_one — effective_values() below turns it
# into an omitted key, never a substituted default.
_NO_USER_VALUE = object()


def personal_value(session: Session, user_id: str, key: str) -> Any:
    """The value of the scope='user' setting ``key`` for ``user_id``, for code
    acting on behalf of one user (a worker, a service): that user's own row,
    else the declared default — never anybody else's row, never a system one.
    The instance's synthetic user (authentication off) is a user like any
    other here: its own row, stored for ``SYSTEM_USER_ID``."""
    setting = get_def(key)
    if setting is None or setting.scope != "user":
        raise ValueError(f"'{key}' is not a personal (scope='user') setting")
    return SettingsRepository(session).get_user(key, user_id, setting.default)


class _GuardFacts:
    """What a ``SettingDef.guard_on`` check may ask about the deployment (``core.settings_catalog.GuardFacts``)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def has_usable_administrator(self) -> bool:
        return user_service.has_usable_administrator(self._session)


class SettingsWriteError(Exception):
    """Sollevato da ``SettingsService.write_values`` su qualunque fallimento
    della pipeline di scrittura. Porta già lo status HTTP e il messaggio di
    dettaglio: la route lo traduce direttamente in una
    ``HTTPException`` senza dover reinterpretare l'errore."""

    def __init__(self, status_code: int, detail: str, error_code: str | None = None) -> None:
        self.status_code = status_code
        self.error_code = error_code
        # A plain text, or ``{error_code, error_message}`` when the failure has a stable code
        # the UI translates (the shape of every other coded API error).
        self.detail: str | dict[str, str] = (
            {"error_code": error_code, "error_message": detail} if error_code else detail
        )
        super().__init__(detail)


class SettingsService:
    """Punto unico da cui le route leggono/scrivono QUALUNQUE impostazione,
    d'app o di provider, rispettando la policy dichiarata in
    ``core/settings_catalog.py`` / ``core/settings_resolver.py``.

    Istanziato per-request (stesso pattern di ``SourceManager``): prende una
    ``Session`` per il ``SettingsRepository`` (impostazioni di provider e
    override per-utente), un ``ConfigManager`` per le impostazioni app-level
    generiche, e il ``ProviderRegistry`` per lo schema unificato e per
    invocare ``validate_config``.
    """

    def __init__(self, session: Session, config: ConfigManager, registry: ProviderRegistry) -> None:
        self._session = session
        self._config = config
        self._registry = registry

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def schema(self, *, include_providers: bool = True) -> list[SettingDef]:
        """Elenco completo (non filtrato per ruolo) delle impostazioni note.
        ``include_providers=False`` restituisce solo il CATALOG core, senza
        alcuna chiave namespaced di provider — usato dai test che esercitano
        il CATALOG in isolamento, senza dover popolare un registry finto."""
        if include_providers:
            return merged_schema(self._registry)
        return list(CATALOG)

    def readable_schema(self, user: User, *, include_providers: bool = True) -> list[SettingDef]:
        """Schema filtrato per ``can_read`` — quello che ``user`` può vedere."""
        return [s for s in self.schema(include_providers=include_providers) if can_read(s, user.role)]

    # ------------------------------------------------------------------
    # Lettura dei valori effettivi
    # ------------------------------------------------------------------

    def effective_values(self, user: User, *, include_providers: bool = True) -> dict[str, Any]:
        """Valore effettivo di OGNI impostazione leggibile da ``user``. Una
        chiave non leggibile è OMESSA dal risultato (mai svuotata) — questa
        omissione è la correzione di sicurezza: oggi un non-admin legge ogni
        impostazione system-scoped, incluse quelle che espongono un
        indirizzo di rete interno e un path del filesystem del server.

        Una chiave scope='user' per cui questo utente non ha MAI scritto una
        riga è OMESSA allo stesso modo (mai riempita col default dichiarato):
        per queste chiavi (es. lingua/tema) l'assenza è un segnale — "non ho
        ancora scelto" — che un valore sostituito distruggerebbe, rendendolo
        indistinguibile da "ho scelto proprio il default". Vedi _read_one."""
        values: dict[str, Any] = {}
        for setting in self.readable_schema(user, include_providers=include_providers):
            value = self._read_one(setting, user)
            if value is _NO_USER_VALUE:
                continue
            values[setting.key] = value
        return values

    def _read_one(self, setting: SettingDef, user: User) -> Any:
        provider_name, _short_key = split_provider_key(setting.key)
        if provider_name is not None:
            # Le impostazioni di provider sono sempre deployment-wide (nessuna
            # cascata utente): una riga system per chiave namespaced, con
            # fallback al default dichiarato se non ancora salvata.
            return self._settings_repo().get_system(setting.key, setting.default)

        if setting.scope == "user":
            # Impostazione puramente personale (es. lingua/tema): non esiste
            # MAI una riga di sistema per queste chiavi (nessuno le scrive a
            # scope 'system', vedi write_values sotto) — solo la riga
            # dell'utente stesso. A differenza di 'system_with_user_override'
            # e delle chiavi di provider, qui il default dichiarato NON è un
            # fallback da restituire: è solo un valore ragionevole per la UI
            # quando non c'è ancora nulla (vedi il commento sopra la loro
            # SettingDef nel CATALOG). Restituire il default renderebbe "mai
            # scelto" indistinguibile da "ho scelto proprio il default" — la
            # distinzione che serve al client per capire quando adottare
            # una preferenza locale del dispositivo una volta sola, senza mai
            # sovrascrivere una scelta reale già salvata. Il sentinel fa
            # sparire la chiave dal risultato di effective_values invece di
            # restituire un valore inventato.
            return self._settings_repo().get_user(setting.key, user.id, _NO_USER_VALUE)

        if setting.type == "list" and setting.provider_category:
            # The order of the providers of a category is never read raw: what is stored may be
            # empty, partial or out of date. The UI sees (and so saves) the effective order.
            return provider_order.effective_provider_order(setting, self._registry, self._config, self._session, user.id)

        if setting.scope == "system_with_user_override":
            # Riga utente (repository) -> valore di sistema. Il livello
            # "sistema" per una chiave app-level passa SEMPRE da
            # ``ConfigManager`` (mai direttamente dal repository): è lui, non
            # questo modulo, a portare il fallback di ultima istanza su
            # config.json (utile finché la 0010 non ha ancora popolato una
            # chiave, o se il DB è momentaneamente irraggiungibile).
            override = self._settings_repo().get_user(setting.key, user.id)
            if override is not None:
                return override

        return self._config.get(setting.key, setting.default)

    # ------------------------------------------------------------------
    # Config grezza di un singolo provider (per GET /providers)
    # ------------------------------------------------------------------

    def provider_config(self, provider_name: str) -> dict[str, Any]:
        """Config SHORT-key già salvata per ``provider_name``, sempre a scope
        di sistema — le impostazioni di provider sono deployment-wide, quindi
        lettura (GET /providers) e scrittura (PATCH /settings) concordano.
        Ricostruita filtrando le righe system per prefisso di chiave
        namespaced: la tabella `settings` memorizza ogni short key come riga
        propria."""
        return provider_config_from_system_settings(provider_name, self._settings_repo().all_system())

    def provider_config_is_valid(self, provider) -> bool:
        """``validate_config`` risponde a "questo provider è COMPLETAMENTE
        configurato e utilizzabile?" — una domanda sull'INTERO provider, che
        appartiene alla LETTURA (``GET /providers``), non alla scrittura di un
        singolo campo (vedi il commento in ``write_values`` sul perché non è un
        gate di scrittura). Un provider il cui ``validate_config`` solleva
        un'eccezione (bug del provider, dipendenza esterna non disponibile,
        ecc.) non deve mai far fallire l'intero elenco: trattalo come "non
        valido", mai come un errore che si propaga."""
        try:
            return bool(provider.validate_config(self.provider_config(provider.name)))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # "usable" a livello di CATEGORIA — per il requisito "provider_category_usable".
    # Riusa la STESSA definizione di "usable" di
    # GET /providers (routes/config.py::_provider_info), mai una seconda.
    # ------------------------------------------------------------------

    def provider_is_usable(self, provider) -> bool:
        """Autenticazione SEMPRE come l'istanza (solo gli account condivisi,
        ``services.credentials.shared_store``): "esiste un provider usable in
        questa categoria" è una domanda sullo stato del deployment, non su chi
        la pone — un non-admin che vede il motivo "richiede un provider X
        usable" non deve poter far scattare un tentativo di login legato al
        proprio account."""
        store = shared_store(self._session)
        auth_manager = auth_manager_for(store)
        try:
            if provider.auth.flow != AuthFlow.NONE:
                auth_manager.authenticate(provider)
            authenticated = True
        except AuthError:
            authenticated = False
        except Exception:
            authenticated = False
        finally:
            try:
                store.rollback()
            except Exception:
                pass

        config_valid = self.provider_config_is_valid(provider)
        return compute_provider_usable(provider, authenticated=authenticated, config_valid=config_valid)

    def category_has_usable_provider(self, category: str) -> bool:
        """True se ALMENO UN provider registrato in ``category`` è usable.
        ``category`` è una stringa dichiarativa (``SettingDef.provider_category``),
        mai un nome di provider — genererica su qualunque categoria che il
        registry conosce (``all_<category>``), identificazione compresa ma
        non SOLO quella."""
        getter = getattr(self._registry, f"all_{category}", None)
        if getter is None:
            return False
        return any(self.provider_is_usable(p) for p in getter())

    # ------------------------------------------------------------------
    # Disponibilità (Requirement) — Task 2: esposta da GET /settings/schema
    # ------------------------------------------------------------------

    def _unfiltered_effective_values(self, user: User, *, include_providers: bool = True) -> dict[str, Any]:
        """Come ``effective_values`` ma SENZA filtro ``can_read`` — serve
        internamente a valutare un requisito "setting_set" che referenzia
        un'impostazione system-scoped (es. ``sync_provider``, admin-only)
        anche quando ``user`` non è admin: il valore stesso non è mai esposto,
        solo se è vuoto o no (tramite il motivo testuale, che nomina la LABEL,
        mai il valore) — vedi ``setting_availability``. Stesso sentinel
        ``_NO_USER_VALUE`` di ``effective_values`` per le chiavi scope='user'
        mai scritte da questo utente."""
        values: dict[str, Any] = {}
        for setting in self.schema(include_providers=include_providers):
            value = self._read_one(setting, user)
            if value is _NO_USER_VALUE:
                continue
            values[setting.key] = value
        return values

    def evaluate_availability(
        self, user: User, *, include_providers: bool = True,
    ) -> dict[str, tuple[bool, str | None]]:
        """{key: (available, reason)} per OGNI impostazione dello schema
        (non solo quelle leggibili da ``user`` — il chiamante filtra dopo).
        Le categorie di provider referenziate da un requisito sono valutate
        una volta sola (cache locale), mai una volta per impostazione."""
        schema = self.schema(include_providers=include_providers)
        schema_by_key = {s.key: s for s in schema}
        values = self._unfiltered_effective_values(user, include_providers=include_providers)

        category_cache: dict[str, bool] = {}

        def category_usable(category: str) -> bool:
            if category not in category_cache:
                category_cache[category] = self.category_has_usable_provider(category)
            return category_cache[category]

        return {
            setting.key: setting_availability(
                setting, effective_values=values, schema_by_key=schema_by_key, category_usable=category_usable,
            )
            for setting in schema
        }

    # ------------------------------------------------------------------
    # Task 3 — enforcement dei requisiti in scrittura (la disponibilità è
    # POLICY: il server la applica, non solo la UI).
    # ------------------------------------------------------------------

    def _enforce_guards(self, coerced: dict[str, Any], schema_by_key: dict[str, SettingDef]) -> None:
        """Rifiuta una scrittura che porta un'impostazione a un valore il cui
        ``guard_on`` dichiarato non passa (es. accendere l'autenticazione con un
        segreto debole, o senza un amministratore che possa accedere). Un solo
        punto per ogni scrittore: spegnere o portare a un altro valore non
        chiama mai il controllo. Nulla è ancora scritto."""
        facts = _GuardFacts(self._session)
        for key, value in coerced.items():
            guard = schema_by_key[key].guard_on
            if guard is None or value != guard[0]:
                continue
            try:
                guard[1](facts)
            except SettingRefused as refused:
                raise SettingsWriteError(400, f"{key}: {refused.message}", refused.error_code) from refused

    def _enforce_requirements(
        self, coerced: dict[str, Any], schema_by_key: dict[str, SettingDef], baseline: dict[str, Any],
    ) -> None:
        """Rifiuta una scrittura che porta un'impostazione NON disponibile a
        un valore diverso dal suo default dichiarato. Valutato contro lo
        stato PROIETTATO — ``baseline`` con l'intero batch già applicato sopra
        — cosi' una richiesta che accende ``sync_provider`` E un'impostazione
        che lo richiede nello STESSO batch va a buon fine, e una che lo
        azzera mentre ne accende una dipendente fallisce. Tornare al default
        dichiarato resta SEMPRE permesso (è l'unico modo per un utente di
        spegnere qualcosa diventato non disponibile)."""
        if not coerced:
            return
        projected = {**baseline, **coerced}
        category_cache: dict[str, bool] = {}

        def category_usable(category: str) -> bool:
            if category not in category_cache:
                category_cache[category] = self.category_has_usable_provider(category)
            return category_cache[category]

        for key, value in coerced.items():
            setting = schema_by_key[key]
            if not setting.requires:
                continue
            if value == setting.default:
                continue
            available, reason = setting_availability(
                setting, effective_values=projected, schema_by_key=schema_by_key, category_usable=category_usable,
            )
            if not available:
                raise SettingsWriteError(400, f"{key}: unavailable — {reason}")

    # ------------------------------------------------------------------
    # Task 4 — un provider_ref è testato PRIMA di essere accettato.
    # ------------------------------------------------------------------

    def _enforce_provider_ref_tests(
        self, coerced: dict[str, Any], schema_by_key: dict[str, SettingDef], baseline: dict[str, Any], user: User,
    ) -> None:
        """Per ogni impostazione ``provider_ref`` nel batch il cui valore
        CAMBIA verso qualcosa di non vuoto, esegue ``test_connection`` del
        provider candidato PRIMA di accettare la scrittura — stessa
        autenticazione/scope di ``/auth/{provider}/test`` (routes/auth.py,
        scope "account": ``user_store`` + ``AuthManager.authenticate``
        + ``provider.test_connection(session)``), riusata qui invece di
        essere riscritta. Svuotare (nuovo valore vuoto) o riscrivere lo stesso
        valore già in vigore non richiede alcun test."""
        for key, value in coerced.items():
            setting = schema_by_key[key]
            if setting.type != "provider_ref":
                continue
            old_value = baseline.get(key)
            if value == old_value or is_empty_value(value):
                continue

            provider = self._get_category_provider(setting.provider_category, value)
            if provider is None:
                # Nome sconosciuto per la categoria: già rifiutato da
                # validate_value più sopra (SettingError) — qui non dovrebbe
                # mai capitare, ma non è comunque compito di questo metodo
                # segnalarlo di nuovo.
                continue

            error = self._test_provider_ref_candidate(provider, user)
            if error is not None:
                raise SettingsWriteError(400, f"{key}: connection test failed for '{value}': {error}")

    def _get_category_provider(self, category: str | None, name: str):
        if not category:
            return None
        getter = getattr(self._registry, f"get_{category}_provider", None)
        if getter is None:
            return None
        return getter(name)

    def _test_provider_ref_candidate(self, provider, user: User) -> str | None:
        """Esegue ``test_connection`` su ``provider`` con lo stesso scope
        credenziali che ``/auth/{provider}/test`` (routes/auth.py) usa per lo
        scope "account". Un'eccezione del provider (bug, dipendenza esterna
        non disponibile, credenziali mancanti) conta come test FALLITO, mai
        come un errore del server."""
        store = user_store(self._session, user.id)
        auth_manager = auth_manager_for(store)
        try:
            session_obj = auth_manager.authenticate(provider)
            return provider.test_connection(session_obj)
        except AuthError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 — un provider che solleva è un fallimento, non un 500
            return str(exc)
        finally:
            try:
                store.rollback()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Scrittura — unità atomica: valida tutto, poi persiste tutto
    # ------------------------------------------------------------------

    def write_values(
        self, raw_updates: dict[str, Any], user: User, *, include_providers: bool = True,
        adopt_into: str | None = None,
    ) -> dict[str, Any]:
        """Applica ``raw_updates`` (chiave -> valore grezzo) come un'unica
        unità atomica: se una qualunque chiave è sconosciuta, vietata per il
        ruolo di ``user``, non coercibile o non valida, l'INTERA richiesta è
        rifiutata e NULLA viene scritto. Solo dopo che tutto è passato si
        persiste, instradando ogni chiave allo storage giusto in base alla
        sua dichiarazione (mai al nome della chiave).

        ``adopt_into``: l'amministratore che riceve i dati del proprietario
        dell'istanza quando QUESTA scrittura accende l'autenticazione
        (``services/auth_transition.py``); ignorato in ogni altro caso."""
        schema_by_key = {s.key: s for s in self.schema(include_providers=include_providers)}

        unknown = sorted(k for k in raw_updates if k not in schema_by_key)
        if unknown:
            raise SettingsWriteError(400, f"Unknown setting(s): {', '.join(unknown)}")

        forbidden = sorted(k for k in raw_updates if not can_write(schema_by_key[k], user.role))
        if forbidden:
            raise SettingsWriteError(
                403, f"Admin role required for: {', '.join(forbidden)}",
            )

        coerced: dict[str, Any] = {}
        for key, raw in raw_updates.items():
            setting = schema_by_key[key]
            try:
                value = coerce_value(setting, raw)
                validate_value(setting, value, self._registry)
            except SettingError as exc:
                raise SettingsWriteError(400, f"{exc.key}: {exc.message}") from exc
            coerced[key] = value

        self._enforce_guards(coerced, schema_by_key)

        # Stato PRIMA di questa scrittura (senza filtro di ruolo: serve a
        # valutare requisiti che referenziano impostazioni system-scoped, es.
        # "sync_provider", anche per un non-admin — vedi
        # _unfiltered_effective_values). Letto una volta sola, riusato sia dal
        # test provider_ref (Task 4, sotto) sia dall'enforcement dei requisiti
        # (Task 3, sotto).
        baseline = self._unfiltered_effective_values(user, include_providers=include_providers)

        self._enforce_provider_ref_tests(coerced, schema_by_key, baseline, user)
        self._enforce_requirements(coerced, schema_by_key, baseline)

        # Raggruppa per destinazione di storage. Nessuna scrittura avviene
        # ancora qui.
        #
        # NOTA: qui NON si chiama ``provider.validate_config(merged)``: non è
        # un gate di scrittura. ``validate_config``
        # risponde a "il provider è COMPLETAMENTE configurato e utilizzabile?",
        # una domanda sull'INTERO provider (spesso richiede credenziali che
        # vivono altrove, es. client id/secret nell'oauth client store, non
        # nella config qui gestita), non "questo singolo campo, coerciso e
        # validato contro la sua dichiarazione, è accettabile?". Farne un gate
        # di scrittura rifiuterebbe aggiornamenti innocui (es. un solo
        # search_limit) su provider reali non ancora completamente configurati
        # o la cui completezza dipende da un default applicato altrove a runtime
        # (es. lo storage provider attivo, che usa un base path di default se
        # assente dalla config salvata). Il contratto vive nella LETTURA
        # (``SettingsService.provider_config_is_valid``, esposto da
        # ``GET /providers``), dove la domanda di completezza è quella giusta
        # da porre.
        provider_updates: dict[str, Any] = {}
        app_level: dict[str, Any] = {}
        user_overrides: dict[str, Any] = {}

        for key, value in coerced.items():
            setting = schema_by_key[key]
            provider_name, _short_key = split_provider_key(key)
            if provider_name is not None:
                # Ogni short key e' gia' la sua riga (namespaced): nessun
                # merge con un blob esistente da fare qui.
                provider_updates[key] = value
            elif setting.scope == "user":
                # Puramente personale: SEMPRE la riga dell'utente che scrive,
                # a prescindere dal ruolo (a differenza di
                # 'system_with_user_override', qui non esiste un default
                # globale da poter scrivere — un admin che scrive la propria
                # lingua non sta configurando il deployment).
                user_overrides[key] = value
            elif setting.scope == "system_with_user_override" and user.role != _ADMIN_ROLE:
                user_overrides[key] = value
            else:
                app_level[key] = value

        # Tutto convalidato: persisti. Accendere l'autenticazione non è una
        # scrittura come le altre: i dati del proprietario passano a un
        # amministratore nella STESSA transazione (auth_transition), e se
        # fallisce non è stato scritto nient'altro del batch.
        if app_level.get(auth_transition.SETTING) is True:
            del app_level[auth_transition.SETTING]
            try:
                auth_transition.turn_on(self._session, self._config, self._registry, administrator_id=adopt_into)
            except auth_transition.TransitionRefused as refused:
                raise SettingsWriteError(400, refused.message, refused.error_code) from refused
        repo = self._settings_repo()
        for key, value in provider_updates.items():
            repo.set_system(key, value, secret=schema_by_key[key].secret)
        if app_level:
            self._config.update(app_level)
        if user_overrides:
            for key, value in user_overrides.items():
                repo.set_user(key, user.id, value, secret=schema_by_key[key].secret)

        return self.effective_values(user, include_providers=include_providers)

    # ------------------------------------------------------------------
    # Helpers interni
    # ------------------------------------------------------------------

    def _settings_repo(self) -> SettingsRepository:
        return SettingsRepository(self._session)
