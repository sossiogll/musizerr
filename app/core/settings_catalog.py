"""
Catalogo dichiarativo delle impostazioni dell'app.

Unica sorgente di verità per OGNI parametro configurabile: niente magic number
sparsi nella logica. I valori di default qui sono *dati editabili* (seed), non
costanti hardcoded nei call-site — il codice legge sempre via ``ConfigManager``.

Il catalogo espone anche i metadati (tipo, area, gruppo, label, descrizione,
vincoli) così il frontend può renderizzare la pagina impostazioni in modo
data-driven, senza mappe hardcoded lato UI.

Provider-agnostico: nessun nome di provider concreto compare qui.

--------------------------------------------------------------------------
Tassonomia dichiarativa (area / gruppo)
--------------------------------------------------------------------------
Oltre al ``group`` (già esistente: un raggruppamento "fine", spesso 1:1 con
un concetto tecnico o un provider), ogni impostazione dichiara ora anche
un ``area``: l'asse "grosso" pensato per l'utente, non per il motore.
Quattro aree, in questo ordine di visualizzazione:

    1. account   — le impostazioni che riguardano la persona che usa l'app.
    2. providers — quali provider esistono, come sono configurati, quale è attivo.
    3. library   — cosa finisce in libreria e come ci arriva.
    4. system    — deployment e concerns dell'operatore.

Ogni gruppo appartiene a ESATTAMENTE un'area (vedi ``GROUP_AREAS``): è
l'invariante che impedisce il problema che questa tassonomia risolve — un
gruppo "generale" che finisce per ospitare impostazioni concettualmente
diverse perché nessuno ha mai dovuto scegliere dove altro metterle.

Le impostazioni dichiarate da un provider (``BaseProvider.config_schema``)
appartengono SEMPRE all'area ``providers``, sotto il gruppo che è il nome del
provider stesso — automaticamente: nessun provider imposta mai il proprio
``area`` a mano (vedi ``settings_resolver.merged_schema``, che lo forza in
fase di merge). Label/descrizione di QUEL gruppo (il provider) non vivono qui
— vengono dalla ``ProviderInfo`` esposta da ``GET /providers``, mai da una
mappa hardcoded nel core.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from . import download_policy, provider_login, user_auth
from musizeer_sdk.capabilities import CAPABILITY_UNLIKE_SAVED_TRACK
from musizeer_sdk.context import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_DOWNLOAD_TIMEOUT,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_STREAM_START_TIMEOUT,
)
from .routines import ROUTINES, enabled_setting_key, interval_setting_key

SettingType = Literal["bool", "int", "float", "string", "enum", "list", "provider_ref"]

# Governance di scrittura di un'impostazione:
#   system                       — deployment-wide, scrivibile solo da admin.
#   user                         — per-utente, ogni utente scrive il proprio valore.
#   system_with_user_override    — default globale che ogni utente può sovrascrivere
#                                   per sé (es. metadata_provider_priority).
SettingScope = Literal["system", "user", "system_with_user_override"]

# Tipo di requisito dichiarativo (vedi Requirement sotto).
RequirementKind = Literal["provider_category_usable", "setting_set"]

# Sentinel per SettingDef.fail_safe: distingue "nessun fail_safe dichiarato"
# da "il fail_safe dichiarato è None" (None può essere un default legittimo,
# es. sync_provider). Mai confrontato per valore, solo per identità.
_NO_FAIL_SAFE = object()


@dataclass(frozen=True)
class Requirement:
    """Un singolo requisito che una SettingDef può porre per essere
    DISPONIBILE — diverso da ``visible_when`` (quello risponde a "va mostrata
    in UI?", questo risponde a "ha senso che sia accesa?"). Due tipi, entrambi
    provider-agnostici (nessun nome di provider concreto compare mai qui):

    - "provider_category_usable": soddisfatto quando ESISTE almeno un
      provider REGISTRATO in ``provider_category`` che è "usable" — stessa
      identica definizione di "usable" già esposta da ``GET /providers``
      (vedi ``settings_resolver.compute_provider_usable``): configurazione
      valida (``validate_config``) e, dove il contratto della categoria offre
      un check di disponibilità aggiuntivo (es. ``IdentificationProvider.
      is_available()``), che quel check passi.
    - "setting_set": soddisfatto quando il valore EFFETTIVO dell'impostazione
      ``setting_key`` non è vuoto (vedi ``settings_resolver.is_empty_value``).
      Usato per esprimere "un certo provider è attivo" quando quella scelta è
      già, essa stessa, un'impostazione ``provider_ref`` (es. "un sync
      provider è attivo" == "l'impostazione 'sync_provider' è valorizzata").
    """

    kind: RequirementKind
    provider_category: str | None = None
    setting_key: str | None = None


def requires_provider_category(category: str) -> Requirement:
    """Requisito: esiste almeno un provider usable nella categoria data."""
    return Requirement(kind="provider_category_usable", provider_category=category)


def requires_setting(key: str) -> Requirement:
    """Requisito: l'impostazione ``key`` ha un valore effettivo non vuoto."""
    return Requirement(kind="setting_set", setting_key=key)


class SettingRefused(Exception):
    """A ``SettingDef.guard_on`` check refuses the value: ``error_code`` is the stable,
    translatable code the API answers with, ``message`` the readable text (never a secret)."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        self.message = message
        super().__init__(message)


class GuardFacts(Protocol):
    """What a ``guard_on`` check may ask about the deployment, answered by the host
    (``services/settings.py``: ``SettingsService._guard_facts``): the catalogue is pure and reads
    no database, so the facts come in as an argument."""

    def has_usable_administrator(self) -> bool:
        """An ACTIVE administrator account a person can sign in to (``db.user_service``)."""
        ...


def _strong_auth_secret(facts: GuardFacts) -> None:  # noqa: ARG001
    """Sessions are signed with ``AUTH_SECRET``: authentication cannot be turned on while it is
    missing, short or a placeholder (``user_auth.require_strong_secret``, the rule startup asks too)."""
    try:
        user_auth.require_strong_secret()
    except user_auth.InsecureAuthSecret as insecure:
        raise SettingRefused("auth_secret_insecure", str(insecure)) from insecure


def _an_administrator_can_sign_in(facts: GuardFacts) -> None:
    """With authentication on every admin route needs an administrator's session: turning it on
    while none can sign in would lock the owner out of the app with no way back in but the
    database. Create one first (Administration › Users, or the guided step of the settings page)."""
    if not facts.has_usable_administrator():
        raise SettingRefused(
            "no_admin_account",
            "Authentication cannot be turned on: no active administrator account exists to sign in with. "
            "Create an administrator first.",
        )


def _authentication_may_turn_on(facts: GuardFacts) -> None:
    """Both conditions of turning authentication on: sessions can be signed, and someone can sign in."""
    _strong_auth_secret(facts)
    _an_administrator_can_sign_in(facts)


@dataclass(frozen=True)
class SettingDef:
    """Definizione di una singola impostazione configurabile."""

    key: str
    type: SettingType
    default: Any
    group: str
    label: str
    description: str = ""
    # Asse grosso, orientato all'utente (vedi il modulo docstring). Per una
    # voce del CATALOG è sempre valorizzato tramite GROUP_AREAS (mai a mano,
    # vedi la factory `_entry` più sotto); per una voce di provider viene
    # forzato in fase di merge (settings_resolver.merged_schema), mai
    # dichiarato dal provider stesso.
    area: str = ""
    # Governance di scrittura (vedi SettingScope). `admin_only` sotto è una
    # property derivata mantenuta per compatibilità con i call-site esistenti.
    scope: SettingScope = "system"
    # Vincoli opzionali (usati dalla UI e per la validazione).
    options: tuple[str, ...] | None = None       # per type == "enum"
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    # Se True il valore è un segreto: la UI non lo mostra in chiaro.
    secret: bool = False
    # Per type == "provider_ref": la categoria di provider referenziata
    # (es. "storage", "sync"). Stringa libera, mai una lista hardcoded.
    provider_category: str | None = None
    # Coppie (altra chiave, valore richiesto) che devono valere TUTTE perché
    # questa impostazione sia mostrata. Tupla di tuple (non dict) perché
    # SettingDef è frozen/hashable.
    visible_when: tuple[tuple[str, Any], ...] | None = None
    # Se False, l'impostazione è opzionale (il provider la dichiara con ``ProviderSetting.required``).
    required: bool = True
    # Requisiti di DISPONIBILITÀ (vedi Requirement sopra) — decidono se questa
    # impostazione ha senso di essere accesa nello stato attuale, non se va
    # mostrata (quello resta visible_when). Valutati da
    # settings_resolver/services.settings e esposti additivamente da
    # GET /settings/schema (available/unavailable_reason), enforcati sulla
    # scrittura (PATCH /settings): vedi services/settings.write_values.
    requires: tuple[Requirement, ...] = ()
    # True per un'impostazione che ha una pagina dedicata (es. i routine di
    # sistema, vedi GROUP_ROUTINES) e che quindi la pagina impostazioni
    # GENERICA deve saltare — resta comunque una SettingDef a pieno titolo
    # (schema/valori/scrittura passano dalla stessa pipeline dichiarativa),
    # solo non va renderizzata li'. Il frontend deve filtrare su questo
    # flag in GET /settings/schema (vedi to_public) invece di riconoscere le
    # chiavi per prefisso.
    hidden: bool = False
    # (valore, messaggio): scegliere questo valore dalla UI chiede prima una
    # conferma esplicita (es. spegnere l'autenticazione). Il messaggio è il
    # testo di ripiego; la UI traduce ``settings.confirm.<key>``.
    confirm_on: tuple[Any, str] | None = None
    # (valore, controllo): una scrittura che porta l'impostazione a ``valore``
    # passa prima dal controllo, che solleva ``SettingRefused`` se il
    # deployment non regge (es. accendere l'autenticazione con un segreto
    # debole). Portarla a un altro valore non lo chiama mai. Mai serializzato.
    guard_on: tuple[Any, Callable[[GuardFacts], None]] | None = None
    # Valore servito da ConfigManager mentre lo store non è MAI stato letto
    # con successo da questa istanza (DB irraggiungibile, o app avviata prima
    # della migrazione) — finestra distinta dal "degrado dopo un successo"
    # (quello mantiene la cache precedente, vedi ConfigManager). Non
    # dichiarato per la maggior parte delle chiavi: quelle usano il default
    # del catalogo anche in quella finestra, comportamento invariato. Va
    # dichiarato solo per un'impostazione il cui default è permissivo dal
    # punto di vista di sicurezza/privacy (vedi auth_enabled). Mai serializzato
    # al frontend (to_public() non lo include).
    fail_safe: Any = _NO_FAIL_SAFE
    # Chiave di capability (``musizeer_sdk.capabilities.CAPABILITY_*``) attraverso cui
    # questa impostazione agisce sui provider: la UI elenca accanto al
    # controllo i provider che la dichiarano (``capabilities`` in GET
    # /providers) e lo stato dell'account dell'utente su ciascuno. Mai un nome
    # di provider.
    provider_capability: str | None = None

    @property
    def admin_only(self) -> bool:
        """Derivato da scope: True solo per le impostazioni system-wide."""
        return self.scope == "system"

    def to_public(self) -> dict[str, Any]:
        """Rappresentazione serializzabile per il frontend (senza valori segreti)."""
        return {
            "key": self.key,
            "type": self.type,
            "group": self.group,
            "area": self.area,
            "label": self.label,
            "description": self.description,
            # Chiavi con cui la UI traduce label e descrizione; il testo del
            # catalogo resta il ripiego quando il dizionario non le ha.
            "label_key": f"settings.labels.{self.key}",
            "description_key": f"settings.descriptions.{self.key}",
            "provider_capability": self.provider_capability,
            "admin_only": self.admin_only,
            "scope": self.scope,
            "options": list(self.options) if self.options else None,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "step": self.step,
            "secret": self.secret,
            "provider_category": self.provider_category,
            "required": self.required,
            "hidden": self.hidden,
            # Chiave di traduzione di ogni opzione di un enum, nello stesso
            # ordine di ``options`` (la UI mostra il valore grezzo solo se la
            # chiave manca).
            "option_label_keys": (
                [f"settings.options.{self.key}.{option}" for option in self.options] if self.options else None
            ),
            "confirm": (
                {
                    "value": self.confirm_on[0],
                    "message_key": f"settings.confirm.{self.key}",
                    "message": self.confirm_on[1],
                }
                if self.confirm_on else None
            ),
            "visible_when": (
                [list(pair) for pair in self.visible_when] if self.visible_when else None
            ),
        }


# ---------------------------------------------------------------------------
# Aree (ordine di visualizzazione lato UI).
# ---------------------------------------------------------------------------
AREA_ACCOUNT = "account"
AREA_PROVIDERS = "providers"
AREA_LIBRARY = "library"
AREA_SYSTEM = "system"

AREA_ORDER: tuple[str, ...] = (AREA_ACCOUNT, AREA_PROVIDERS, AREA_LIBRARY, AREA_SYSTEM)

AREA_LABELS: dict[str, str] = {
    AREA_ACCOUNT: "Account",
    AREA_PROVIDERS: "Provider",
    AREA_LIBRARY: "Libreria",
    AREA_SYSTEM: "Sistema",
}


# ---------------------------------------------------------------------------
# Gruppi (raggruppamento fine, dentro un'area).
# ---------------------------------------------------------------------------
GROUP_ACCOUNT = "account"
GROUP_PROVIDER_SELECTION = "provider_selection"
GROUP_FILES = "files"
GROUP_SEARCH = "search"
GROUP_DOWNLOADS = "downloads"
# Which download requests wait for an admin (core/download_policy.py).
GROUP_DOWNLOAD_REQUESTS = "download_requests"
GROUP_INTEGRITY = "integrity"
GROUP_RECONCILIATION = "reconciliation"
# Identificazione e deduplicazione sono la stessa preoccupazione ("non tenere
# due copie della stessa traccia"): un gruppo solo, vedi la sua GroupMeta più
# sotto.
GROUP_IDENTIFICATION = "identification"
GROUP_LIBRARY_SYNC = "library_sync"
# The playlists users keep in Musizeer (services/playlists.py).
GROUP_PLAYLISTS = "playlists"
GROUP_AUTH = "auth"
# Configurazione (enabled/intervallo) dei "system routines" — generata da
# core/routines.py (RoutineDef), mai scritta a mano voce per voce (vedi
# _routine_settings più sotto). `hidden=True` su queste voci: la pagina
# impostazioni generica le salta, la futura pagina "System routines" le
# possiede.
GROUP_ROUTINES = "routines"

# Ogni gruppo del CATALOG appartiene a ESATTAMENTE un'area — unica sorgente
# di verità, usata sia per costruire ``area`` di ogni voce del CATALOG (vedi
# `_entry`) sia dai test come invariante strutturale. I gruppi di provider
# (nome del provider stesso) NON compaiono qui: la loro area è sempre
# AREA_PROVIDERS, forzata in ``settings_resolver.merged_schema``.
GROUP_AREAS: dict[str, str] = {
    GROUP_ACCOUNT: AREA_ACCOUNT,
    GROUP_PROVIDER_SELECTION: AREA_PROVIDERS,
    GROUP_FILES: AREA_LIBRARY,
    GROUP_DOWNLOADS: AREA_LIBRARY,
    GROUP_DOWNLOAD_REQUESTS: AREA_LIBRARY,
    GROUP_INTEGRITY: AREA_LIBRARY,
    GROUP_IDENTIFICATION: AREA_LIBRARY,
    GROUP_LIBRARY_SYNC: AREA_LIBRARY,
    GROUP_PLAYLISTS: AREA_LIBRARY,
    GROUP_AUTH: AREA_SYSTEM,
    GROUP_SEARCH: AREA_SYSTEM,
    GROUP_RECONCILIATION: AREA_SYSTEM,
    GROUP_ROUTINES: AREA_SYSTEM,
}


@dataclass(frozen=True)
class GroupMeta:
    """Metadati dichiarativi di un gruppo del CATALOG (non di provider)."""

    key: str
    label: str
    description: str
    area: str


# Ordine di visualizzazione: raggruppati per area (nell'ordine di AREA_ORDER),
# e dentro ciascuna area nell'ordine dichiarato qui. Questo tuple È l'ordine
# esplicito richiesto — nessun ordinamento implicito da un dict o da un set.
GROUPS: tuple[GroupMeta, ...] = (
    # ----- account -----
    GroupMeta(
        GROUP_ACCOUNT, "Account",
        "Preferenze personali di chi ha effettuato l'accesso: lingua, tema, "
        "comportamento della libreria per il proprio account.",
        AREA_ACCOUNT,
    ),
    # ----- providers -----
    GroupMeta(
        GROUP_PROVIDER_SELECTION, "Selezione provider",
        "Quale provider è attivo per ciascun ruolo (storage, sincronizzazione, "
        "ordine di tentativo dei download).",
        AREA_PROVIDERS,
    ),
    # ----- library -----
    GroupMeta(
        GROUP_FILES, "File",
        "Dove e come vengono salvati i file scaricati: cartella, copertine, "
        "formato del nome file.",
        AREA_LIBRARY,
    ),
    GroupMeta(
        GROUP_DOWNLOADS, "Download",
        "Concorrenza, timeout e retry del worker che esegue i download.",
        AREA_LIBRARY,
    ),
    GroupMeta(
        GROUP_DOWNLOAD_REQUESTS, "Richieste di download",
        "Quali download chiesti dagli utenti partono subito e quali aspettano l'approvazione di un "
        "amministratore, per tipo di richiesta, e quante tracce può far scaricare ogni utente in una "
        "finestra di giorni. Gli amministratori non sono mai soggetti a queste regole; l'amministratore "
        "può cambiarle per un singolo utente.",
        AREA_LIBRARY,
    ),
    GroupMeta(
        GROUP_INTEGRITY, "Integrità",
        "Verifica che ogni file scaricato sia valido e completo.",
        AREA_LIBRARY,
    ),
    GroupMeta(
        GROUP_IDENTIFICATION, "Identificazione audio e deduplicazione",
        "Riconoscimento dell'audio via fingerprint ed eliminazione delle copie "
        "duplicate in libreria. Oggi le due cose sono indipendenti: la "
        "deduplicazione risolve i conflitti segnalati da un sistema di "
        "download esterno confrontando la qualità dichiarata dei file, senza "
        "riconoscere l'audio. La deduplicazione guidata dall'identità (stesso "
        "recording riconosciuto via fingerprint) è progettata ma non ancora "
        "implementata: abilitare l'identificazione qui sotto non cambia, oggi, "
        "come i duplicati vengono risolti.",
        AREA_LIBRARY,
    ),
    GroupMeta(
        GROUP_LIBRARY_SYNC, "Sincronizzazione libreria",
        "Ogni quanto musizeer importa le tracce salvate/piaciute dai metadata "
        "provider, e come verifica separatamente che la libreria sia "
        "allineata al sync provider: due percorsi indipendenti che condividono "
        "questo gruppo.",
        AREA_LIBRARY,
    ),
    GroupMeta(
        GROUP_PLAYLISTS, "Playlist",
        "Le playlist che gli utenti tengono in Musizeer (proprie o seguite da un provider di metadati).",
        AREA_LIBRARY,
    ),
    # ----- system -----
    GroupMeta(
        GROUP_AUTH, "Autenticazione",
        "Login, registrazione e accesso anonimo al deployment.",
        AREA_SYSTEM,
    ),
    GroupMeta(
        GROUP_SEARCH, "Ricerca",
        "Limiti e budget di tempo delle ricerche fra i provider di metadati.",
        AREA_SYSTEM,
    ),
    GroupMeta(
        GROUP_RECONCILIATION, "Riconciliazione delegati",
        "Come musizeer apprende l'esito di un download delegato a un sistema "
        "esterno (webhook, poll, auto-like).",
        AREA_SYSTEM,
    ),
    GroupMeta(
        GROUP_ROUTINES, "System routines",
        "Ogni quanto girano i job periodici di musizeer, e se sono abilitati. "
        "Gestiti dalla pagina dedicata (stato ultima esecuzione, avvio manuale), "
        "non da questa lista generica di impostazioni.",
        AREA_SYSTEM,
    ),
)

_GROUPS_BY_KEY: dict[str, GroupMeta] = {g.key: g for g in GROUPS}

# The folder the host and the provider gateway exchange files through. The gateway mounts it in its
# sandboxes and reads its own copy of the path from ``GATEWAY_WORKSPACE_DIR``; the deployment (docker-compose.yaml)
# sets BOTH from ONE variable, ``WORKSPACE_DIR``, so the default of the ``workspace_dir`` setting is read from it.
# A value saved in the settings wins over this seed, and the host warns when it differs from the gateway's
# (``services/provider_events.GatewayWatcher``).
DEFAULT_WORKSPACE_DIR = "/downloads/.musizeer-work"
WORKSPACE_DIR_ENVIRONMENT = "WORKSPACE_DIR"


def default_workspace_dir() -> str:
    return os.environ.get(WORKSPACE_DIR_ENVIRONMENT, "").strip() or DEFAULT_WORKSPACE_DIR


def _entry(
    key: str,
    type_: SettingType,
    default: Any,
    group: str,
    label: str,
    description: str = "",
    **kwargs: Any,
) -> SettingDef:
    """Factory per le voci del CATALOG (mai per le voci di provider): deriva
    ``area`` automaticamente da ``group`` tramite ``GROUP_AREAS``, così
    un'impostazione non può mai finire filata in un gruppo senza che l'area
    corretta segua a ruota — l'errore (un'area scelta a mano, incoerente col
    gruppo) non può proprio accadere, non solo "viene beccato da un test"."""
    return SettingDef(key, type_, default, group, label, description, area=GROUP_AREAS[group], **kwargs)


def _routine_settings() -> tuple[SettingDef, ...]:
    """Genera due SettingDef per OGNI routine dichiarato in
    ``core/routines.py`` (``routine.<key>.enabled``, bool;
    ``routine.<key>.interval_minutes``, int con ``minimum`` = il minimo
    dichiarato dal routine) — aggiungere un routine è un'unica riga in
    ``core/routines.py``, mai una SettingDef scritta a mano qui.

    ``hidden=True``: proprietà della futura pagina "System routines", non
    della pagina impostazioni generica (vedi SettingDef.hidden). Label e
    descrizione sono solo un fallback inglese breve — il frontend localizza
    per chiave del routine, mai da questo testo.
    """
    entries: list[SettingDef] = []
    for routine in ROUTINES:
        entries.append(_entry(
            enabled_setting_key(routine.key), "bool", routine.default_enabled, GROUP_ROUTINES,
            f"Routine '{routine.key}' enabled",
            f"Whether the '{routine.key}' system routine runs on its schedule.",
            hidden=True,
        ))
        entries.append(_entry(
            interval_setting_key(routine.key), "int", routine.default_interval_minutes, GROUP_ROUTINES,
            f"Routine '{routine.key}' interval (minutes)",
            f"How often, in minutes, the '{routine.key}' system routine runs.",
            minimum=routine.min_interval_minutes, hidden=True,
        ))
    return tuple(entries)


_KIND_LABELS = {
    download_policy.KIND_TRACK: ("una traccia", "singola traccia"),
    download_policy.KIND_ALBUM: ("un album", "album"),
    download_policy.KIND_PLAYLIST: ("una playlist", "playlist"),
    download_policy.KIND_ARTIST: ("tutto di un artista", "discografia di un artista"),
    download_policy.KIND_LIKED_ELSEWHERE: (
        "i brani piaciuti altrove", "download dei brani piaciuti su altri provider",
    ),
}


def _download_request_settings() -> tuple[SettingDef, ...]:
    """Le impostazioni della policy di approvazione dei download
    (``core/download_policy.py``): una modalità e una soglia per ogni tipo di
    richiesta, più la quota per utente. Generate da ``download_policy.KINDS``:
    un nuovo tipo di richiesta è una riga lì, mai una SettingDef scritta a
    mano. Solo admin (``scope="system"``): l'override per un singolo utente
    non passa dalle impostazioni, vedi ``services/download_requests.py``."""
    entries: list[SettingDef] = []
    for kind in download_policy.KINDS:
        title, noun = _KIND_LABELS[kind]
        rule = download_policy.DEFAULT_RULES[kind]
        entries.append(_entry(
            download_policy.mode_key(kind), "enum", rule.mode, GROUP_DOWNLOAD_REQUESTS,
            f"Richiesta di {title}: modalità",
            f"Cosa succede quando un utente chiede {noun}: parte subito (auto), aspetta sempre un "
            f"amministratore (approval) oppure parte subito solo fino a una soglia di tracce da scaricare "
            f"(auto_up_to).",
            options=download_policy.MODES,
        ))
        entries.append(_entry(
            download_policy.threshold_key(kind), "int", rule.threshold, GROUP_DOWNLOAD_REQUESTS,
            f"Richiesta di {title}: soglia (tracce)",
            f"Con la modalità auto_up_to: il massimo di tracce da scaricare per cui la richiesta "
            f"({noun}) parte subito. Le tracce già in libreria o già in arrivo non contano.",
            minimum=1, maximum=100000, step=1,
            visible_when=((download_policy.mode_key(kind), download_policy.MODE_AUTO_UP_TO),),
        ))
    entries.append(_entry(
        download_policy.QUOTA_TRACKS_KEY, "int", download_policy.DEFAULT_QUOTA_TRACKS, GROUP_DOWNLOAD_REQUESTS,
        "Quota per utente (tracce)",
        "Quante tracce possono mettere in coda, in una finestra di giorni, le richieste approvate di un "
        "utente. Una richiesta che supererebbe la quota aspetta un amministratore, qualunque sia la sua "
        "modalità. 0 = nessuna quota.",
        minimum=0, maximum=1000000, step=10,
    ))
    entries.append(_entry(
        download_policy.QUOTA_DAYS_KEY, "int", download_policy.DEFAULT_QUOTA_DAYS, GROUP_DOWNLOAD_REQUESTS,
        "Finestra della quota (giorni)",
        "Quanti giorni indietro conta la quota: una finestra mobile, non un azzeramento a data fissa.",
        minimum=1, maximum=365, step=1,
    ))
    return tuple(entries)


# ---------------------------------------------------------------------------
# Il catalogo. I default preesistenti restano identici a prima (nessun cambio
# di comportamento, nessun cambio di valore, tipo, scope o vincolo): quello
# che cambia in questo passaggio è SOLO la tassonomia (group/area) sotto cui
# ogni voce è filata.
# ---------------------------------------------------------------------------

CATALOG: tuple[SettingDef, ...] = (
    # ======================= area: account =======================
    # Per-utente. Nessun default system-wide
    # per language/theme: la LORO assenza per un utente è un segnale, non un
    # dato mancante da colmare — significa "non ho ancora scelto", e il
    # frontend decide dal browser (lingua/tema di sistema). Un default
    # dichiarato qui serve solo come valore di fallback ragionevole per chi
    # non ha mai scelto nulla, mai come riga system.
    _entry("language", "enum", "en", GROUP_ACCOUNT,
           "Lingua", "Lingua dell'interfaccia. Se non impostata, si usa quella del browser.",
           scope="user", options=("en", "it")),
    _entry("theme", "enum", "system", GROUP_ACCOUNT,
           "Tema", "Aspetto dell'interfaccia: chiaro, scuro o quello del sistema operativo.",
           scope="user", options=("light", "dark", "system")),
    # Sfondo animato dell'interfaccia: solo una preferenza di aspetto, scelta
    # da ciascuno (scope 'user', come tema e lingua). Spento di default: è
    # decorazione che costa CPU/GPU, quindi si accende per scelta. Il frontend
    # lo disegna solo su desktop (puntatore preciso, viewport larga) e mai con
    # prefers-reduced-motion in movimento; in modalità aperta (nessun account
    # a cui attribuirlo) resta spento. Le opzioni sono nomi di effetti grafici
    # dell'interfaccia, non di provider.
    _entry("background", "enum", "off", GROUP_ACCOUNT,
           "Sfondo animato",
           "Sfondo animato dietro l'interfaccia, solo su desktop: spento, aurora, onde, spettro o tipografia. "
           "Si ferma con la scheda in background e rispetta la preferenza di movimento ridotto del sistema.",
           scope="user", options=("off", "aurora", "waves", "spectrum", "typography")),
    # Scope system_with_user_override: è sia una preferenza personale sia un
    # default globale — un admin che lo edita sta configurando il default per
    # chi non ha ancora scelto. Vive in `account` perché è lì che una persona
    # la incontra; dove e come un admin edita il default è una scelta della
    # UI, non di questa tassonomia.
    _entry("metadata_provider_priority", "list", [], GROUP_ACCOUNT,
           "Priorità metadata provider", "Ordine di priorità dei provider di metadati.",
           scope="system_with_user_override", provider_category="metadata"),
    # La rimozione di un preferito si ricorda sempre (RemovedFavorite): questa
    # voce decide solo se il like viene tolto ANCHE dal provider d'origine
    # (dove l'API lo permette: CAPABILITY_UNLIKE_SAVED_TRACK). Scope 'user',
    # non 'system_with_user_override': agisce sull'account personale di chi
    # toglie il preferito, quindi è una scelta che solo quella persona può
    # fare — un default deciso da un admin agirebbe sugli account di altri
    # senza che l'abbiano scelto. In modalità aperta non c'è un utente a cui
    # attribuirla: resta spenta.
    _entry("unlike_on_origin_providers", "bool", False, GROUP_ACCOUNT,
           "Togli il like anche dal provider d'origine",
           "Quando togli un preferito, toglie il like anche sul tuo account dei provider da cui era stato "
           "importato, solo dove l'API lo permette. Agisce soltanto sui tuoi account collegati, mai su "
           "quelli condivisi.",
           scope="user", provider_capability=CAPABILITY_UNLIKE_SAVED_TRACK),

    # ======================= area: providers =======================
    # Nessun default: il catalogo non nomina provider (Golden Rule). Finché non
    # se ne sceglie uno non c'è storage (download, stream e scansione lo
    # gestiscono come "nessuno storage"); la migrazione 0040 ha scritto il
    # valore alle installazioni che si affidavano al vecchio default.
    _entry("storage_provider", "provider_ref", None, GROUP_PROVIDER_SELECTION,
           "Storage provider", "Provider di storage attivo.", provider_category="storage",
           required=False),
    _entry("sync_provider", "provider_ref", None, GROUP_PROVIDER_SELECTION,
           "Sync provider", "Provider di sincronizzazione attivo (opzionale).", provider_category="sync",
           required=False),
    # `provider_category` qui non serve a validate_value/coerce_value (un
    # "list" non è convalidato contro l'elenco provider come un
    # "provider_ref" — è un ordine, non una singola selezione) ma è la stessa
    # informazione dichiarativa di cui la UI ha bisogno per sapere QUALE
    # categoria di GET /providers questo ordine riguarda, così il frontend
    # non tiene una propria mappa chiave->categoria. Dichiarare la categoria
    # fa anche di questa lista un ORDINE di provider (`provider_order_settings`):
    # il valore salvato è una preferenza, chi legge usa l'ordine effettivo di
    # `services/provider_order.py`.
    _entry("download_provider_order", "list", [], GROUP_PROVIDER_SELECTION,
           "Ordine download provider", "Ordine di tentativo dei provider di download.",
           provider_category="download"),

    # ======================= area: library =======================
    # ----- File -----
    _entry("workspace_dir", "string", default_workspace_dir(), GROUP_FILES,
           "Cartella di lavoro",
           "Dove l'host crea le cartelle temporanee in cui i provider scrivono e leggono i file. Sullo stesso volume della libreria, spostare un file è istantaneo. Deve essere la stessa cartella del gateway (WORKSPACE_DIR / GATEWAY_WORKSPACE_DIR): cambiarla qui senza cambiarla anche là scollega i provider dai loro file."),
    _entry("held_upload_max_age_hours", "int", 72, GROUP_FILES,
           "Conservazione file in attesa di upload (h)",
           "Un file scaricato il cui salvataggio sullo storage è fallito resta nella cartella di lavoro e si ritenta solo l'upload; oltre queste ore viene eliminato (il download si rifarà da capo).",
           minimum=1, maximum=720, step=1),
    _entry("save_cover", "bool", True, GROUP_FILES,
           "Salva copertine", "Scarica e salva la cover art insieme alla traccia."),
    _entry("track_format", "string",
           "{artist}/{album}/{track_number:02d}. {title}", GROUP_FILES,
           "Formato file traccia",
           "Template str.format(): {artist} {album} {title} {track_number} {release_year}."),
    _entry("stream_start_timeout", "int", DEFAULT_STREAM_START_TIMEOUT, GROUP_FILES,
           "Timeout avvio ascolto (s)",
           "Quanto si aspetta lo storage per ogni passo prima del primo byte di un ascolto (connessione, accesso, apertura del trasferimento). Breve: chi ascolta sta aspettando, e se lo storage non risponde si passa alla copia del sync provider.",
           minimum=1, maximum=60, step=1),
    _entry("storage_stream_cooldown_seconds", "int", 120, GROUP_FILES,
           "Pausa storage dopo un ascolto fallito (s)",
           "Dopo uno storage irraggiungibile durante un ascolto, per questo tempo si ascolta direttamente la copia del sync provider (se c'è) senza riprovare lo storage. 0 = mai.",
           minimum=0, maximum=3600, step=10),

    # ----- Download / worker -----
    _entry("max_concurrent_downloads", "int", 2, GROUP_DOWNLOADS,
           "Download concorrenti", "Numero massimo di download in parallelo (worker_concurrency).",
           minimum=1, maximum=16, step=1),
    _entry("max_retries", "int", 5, GROUP_DOWNLOADS,
           "Retry massimi", "Tentativi massimi per un fallimento *transitorio* (i not-found falliscono subito).",
           minimum=0, maximum=10, step=1),
    _entry("retry_delay_seconds", "int", 60, GROUP_DOWNLOADS,
           "Ritardo retry (s)",
           "Base del backoff esponenziale fra i tentativi: raddoppia a ogni tentativo, con un po' di casualità.",
           minimum=0, maximum=3600, step=1),
    _entry("retry_max_delay_seconds", "int", 3600, GROUP_DOWNLOADS,
           "Ritardo retry massimo (s)", "Tetto dell'attesa fra due tentativi.",
           minimum=0, maximum=86400, step=60),
    _entry("provider_cooldown_errors", "int", 5, GROUP_DOWNLOADS,
           "Pausa provider dopo N errori",
           "Dopo questi errori transitori consecutivi un provider di download viene saltato per un po': i download che lo trovano in pausa aspettano la fine della pausa invece di consumare i loro tentativi. 0 = mai.",
           minimum=0, maximum=100, step=1),
    _entry("provider_cooldown_seconds", "int", 900, GROUP_DOWNLOADS,
           "Durata pausa provider (s)", "Quanto riposa un provider di download dopo troppi errori consecutivi.",
           minimum=60, maximum=86400, step=60),
    _entry("transient_retry_after_hours", "int", 6, GROUP_DOWNLOADS,
           "Riprova fallimenti transitori dopo (h)",
           "Il routine dei fallimenti transitori riprova una traccia fallita per un errore temporaneo dopo queste ore.",
           minimum=1, maximum=24 * 30, step=1),
    _entry("transient_retry_max_rounds", "int", 3, GROUP_DOWNLOADS,
           "Giri massimi per i fallimenti transitori",
           "Quante volte il routine riprova la stessa traccia, prima di lasciarla fallita.",
           minimum=1, maximum=20, step=1),
    _entry("transient_retry_batch_size", "int", 25, GROUP_DOWNLOADS,
           "Tracce per giro",
           "Quante tracce il routine rimette in coda ogni volta, per non riversare un intero fallimento di massa sui provider.",
           minimum=1, maximum=500, step=1),
    _entry("http_timeout", "int", DEFAULT_HTTP_TIMEOUT, GROUP_DOWNLOADS,
           "Timeout HTTP (s)", "Timeout per le richieste API di metadati.",
           minimum=1, maximum=120, step=1),
    _entry("download_timeout", "int", DEFAULT_DOWNLOAD_TIMEOUT, GROUP_DOWNLOADS,
           "Timeout download (s)", "Timeout per il download del singolo file.",
           minimum=5, maximum=1800, step=5),
    _entry("connect_timeout", "int", DEFAULT_CONNECT_TIMEOUT, GROUP_DOWNLOADS,
           "Timeout connessione (s)",
           "Timeout per aprire una connessione a un servizio di file (es. lo storage FTP). Breve: un servizio irraggiungibile non tiene fermo il worker.",
           minimum=1, maximum=120, step=1),
    _entry("download_soft_time_limit_seconds", "int", 240, GROUP_DOWNLOADS,
           "Soft time-limit download (s)",
           "Oltre questo tempo il task tenta di interrompersi con un'eccezione catturabile.",
           minimum=30, maximum=3600, step=10),
    _entry("download_hard_time_limit_seconds", "int", 300, GROUP_DOWNLOADS,
           "Hard time-limit download (s)",
           "Rete di sicurezza: oltre questo tempo il child del worker viene ucciso.",
           minimum=60, maximum=3600, step=10),
    _entry("provider_slot_wait_seconds", "int", 120, GROUP_DOWNLOADS,
           "Attesa posto provider (s)",
           "Quanto un download attende che un provider con limite di concorrenza si liberi, prima di passare al provider successivo.",
           minimum=0, maximum=1800, step=10),

    # ----- Integrity -----
    _entry("verify_integrity_enabled", "bool", True, GROUP_INTEGRITY,
           "Verifica integrità", "Verifica che ogni file scaricato sia decodificabile (ffmpeg) prima di accettarlo."),
    _entry("verify_duration_tolerance_seconds", "int", 5, GROUP_INTEGRITY,
           "Tolleranza durata (s)",
           "Scarto massimo fra durata attesa (metadati) e durata reale del file prima di considerarlo troncato.",
           minimum=0, maximum=60, step=1),
    _entry("verify_after_upload", "enum", "size", GROUP_INTEGRITY,
           "Verifica post-upload",
           "Ricontrollo del file dopo il salvataggio su storage: off | size (dimensione) | full (stream-back + ffmpeg).",
           options=("off", "size", "full")),

    # ----- Identification & dedup (un solo gruppo, vedi la sua GroupMeta
    # sopra) -----
    # La config SPECIFICA del provider (es. la chiave del suo servizio) vive
    # nel provider stesso (config_schema), NON qui. Qui solo i parametri generici.
    # requires=(requires_provider_category("identification"),): confermato
    # leggendo app/workers/tasks.py — ``_check_mislabel`` cerca un provider
    # ``registry.all_identification()`` il cui ``is_available()`` sia True e,
    # se non lo trova, e' un no-op (``return True, None``); la soglia di
    # confidenza e' applicata dall'host sull'identità restituita dal provider
    # (``services.identification.confident``), quindi mai raggiunta senza un
    # provider usable.
    #
    # ATTENZIONE: ``is_available()`` di un provider di identificazione NON
    # deve MAI dipendere da
    # ``identification_enabled`` — se lo facesse, "esiste un provider usable"
    # sarebbe falso ogni volta che il toggle e' spento, e accendere
    # ``identification_enabled`` (che richiede appunto un provider usable)
    # diventerebbe impossibile. ``is_available()`` di un provider consulta
    # solo le sue risorse e la sua config, mai il toggle.
    _entry("identification_enabled", "bool", False, GROUP_IDENTIFICATION,
           "Identificazione audio", "Abilita l'identificazione via fingerprint (richiede un provider configurato).",
           requires=(requires_provider_category("identification"),)),
    _entry("identification_confidence_threshold", "float", 0.90, GROUP_IDENTIFICATION,
           "Soglia di confidenza", "Score minimo per accettare in automatico un match di fingerprint.",
           minimum=0.0, maximum=1.0, step=0.01,
           requires=(requires_provider_category("identification"),)),
    # requires_setting("identification_enabled"): il call site generico
    # (workers/tasks.py::_should_run_anti_mislabel) richiede
    # "config.anti_mislabel_enabled and config.identification_enabled", così
    # l'anti-mislabel rispetta il toggle globale di identificazione senza che
    # un provider lo legga.
    _entry("anti_mislabel_enabled", "bool", False, GROUP_IDENTIFICATION,
           "Anti-mislabel post-download",
           "Dopo il download, verifica che l'audio corrisponda a titolo/artista richiesti (richiede identificazione).",
           requires=(requires_provider_category("identification"), requires_setting("identification_enabled"))),
    _entry("dedup_enabled", "bool", False, GROUP_IDENTIFICATION,
           "Dedup libreria", "Abilita la deduplicazione per-traccia (tiene la qualità migliore)."),
    _entry("dedup_prefer_lossless", "bool", True, GROUP_IDENTIFICATION,
           "Preferisci lossless", "A parità di traccia, il lossless batte sempre il lossy."),

    # ----- Library sync -----
    # L'intervallo di "sync_all_users_saved_tracks" e' ora un routine (vedi
    # GROUP_ROUTINES / core/routines.py: routine.sync_saved_tracks.*) —
    # non piu' una voce a se' qui.
    # requires_setting("sync_provider"): confermato in
    # app/workers/maintenance.py::verify_full_sync — "sync_name =
    # sync_name or config.sync_provider; if not sync_name: return {'status':
    # 'error', ...}" prima di consultare timeout/interval del probe.
    _entry("playlist_max_tracks", "int", 500, GROUP_PLAYLISTS,
           "Tracce per playlist", "Numero massimo di tracce di una playlist, propria o seguita: oltre questo limite non si aggiungono tracce e una playlist più lunga non si può seguire.",
           minimum=1, maximum=10000, step=10),
    _entry("full_sync_probe_timeout_seconds", "int", 120, GROUP_LIBRARY_SYNC,
           "Timeout probe full-sync (s)",
           "Quanto attendere che il file di prova compaia nel sync provider prima di concludere.",
           minimum=10, maximum=1800, step=5, requires=(requires_setting("sync_provider"),)),
    _entry("full_sync_probe_interval_seconds", "int", 5, GROUP_LIBRARY_SYNC,
           "Intervallo poll probe (s)", "Ogni quanto ricontrollare la presenza del file di prova.",
           minimum=1, maximum=60, step=1, requires=(requires_setting("sync_provider"),)),

    # ======================= area: system =======================
    # ----- Auth -----
    # fail_safe: mentre lo store non è mai stato letto con successo, il
    # default di catalogo (False) renderebbe ogni richiesta anonima admin
    # (vedi routes/deps.py::get_current_user) — fail-open. fail_safe=True
    # chiude quella finestra: nessun accesso finché lo store non è leggibile.
    _entry("auth_enabled", "bool", False, GROUP_AUTH,
           "Autenticazione", "Richiedi login per accedere all'app.",
           fail_safe=True,
           guard_on=(True, _authentication_may_turn_on),
           confirm_on=(False, "Senza autenticazione chiunque raggiunga questo indirizzo "
                              "può usare e configurare tutto, come amministratore.")),
    # fail_safe esplicito benché uguale al default: documenta l'intento (se
    # in futuro il default cambiasse, questa riga non lo seguirebbe da sola).
    _entry("registration_open", "bool", True, GROUP_AUTH,
           "Registrazione aperta", "Consenti la registrazione di nuovi account.",
           fail_safe=False),
    # System scope, closed by default: a new account out of a provider's sign-in
    # is something an admin opens on purpose (core/provider_login.py).
    _entry(provider_login.POLICY_SETTING_KEY, "enum", provider_login.DEFAULT_POLICY, GROUP_AUTH,
           "Nuovi account da accesso con provider",
           "Cosa succede quando qualcuno accede con un account di un provider che nessun utente ha collegato: "
           "disabled (rifiutato), pending (si crea un account che attende un amministratore) oppure active "
           "(si crea un account attivo). Chi ha già collegato il provider accede sempre; il ruolo admin non "
           "viene mai da un provider.",
           options=provider_login.POLICIES,
           fail_safe=provider_login.DEFAULT_POLICY),
    _entry("token_expiry_buffer", "int", 60, GROUP_AUTH,
           "Buffer scadenza token (s)", "Secondi prima della scadenza per rinnovare un token.",
           minimum=0, maximum=600, step=5),

    # ----- Ricerca -----
    _entry("search_default_limit", "int", 25, GROUP_SEARCH,
           "Risultati per provider", "Numero massimo di risultati richiesti a ogni provider (fallback se il provider non ha un limite proprio).",
           minimum=1, maximum=100, step=1),
    _entry("search_provider_budget_seconds", "float", 5.0, GROUP_SEARCH,
           "Budget ricerca (s)", "Tempo massimo di attesa dell'insieme dei provider: uno lento non blocca la ricerca (risultati parziali).",
           minimum=1.0, maximum=30.0, step=0.5),
    _entry("enrichment_budget_seconds", "float", 4.0, GROUP_SEARCH,
           "Budget arricchimento (s)", "Tempo massimo che una risposta (ricerca, traccia, album, artista) dedica ad arricchirsi con gli altri provider e con la disponibilità nel sync provider: allo scadere risponde con ciò che ha, e un provider che fallisce viene saltato per il resto della richiesta.",
           minimum=0.5, maximum=30.0, step=0.5),
    _entry("search_stream_enrich_workers", "int", 8, GROUP_SEARCH,
           "Worker enrichment stream", "Ricerche di disponibilità nel sync provider eseguite in parallelo durante l'arricchimento dei risultati.",
           minimum=1, maximum=32, step=1),
    # NIENTE requires_setting("sync_provider") qui: letto
    # app/services/enrichment.py::_enrich_with_stream_url, il TTL è
    # consultato per OGNI provider in ``registry.all_sync()`` — TUTTI i sync
    # provider REGISTRATI, non solo quello selezionato dall'impostazione
    # "sync_provider". Un deployment con un sync provider installato ma non
    # ancora scelto come attivo eserciterebbe comunque questo path: dichiarare
    # il requisito sarebbe falso.
    _entry("sync_availability_cache_ttl_seconds", "int", 180, GROUP_SEARCH,
           "Cache disponibilità sync (s)", "Per quanto tenere in cache l'esito 'disponibile nel sync' di una traccia, per non ripetere la ricerca.",
           minimum=0, maximum=3600, step=10),

    # ----- Reconciliation -----
    _entry("delegation_webhook_enabled", "bool", True, GROUP_RECONCILIATION,
           "Webhook delegati", "Accetta notifiche push dai download provider esterni (es. un webhook del sistema esterno)."),
    _entry("delegation_poll_interval_seconds", "int", 120, GROUP_RECONCILIATION,
           "Intervallo poll delega (s)", "Ogni quanto ripollare lo stato di un download delegato (fallback al webhook).",
           minimum=15, maximum=3600, step=5),
    # requires_setting("sync_provider"): governano solo la propagazione del
    # like al sync provider (i marcatori PendingLike con ``sync_provider``,
    # vedi workers/maintenance.reconcile_like): senza un sync provider attivo
    # il reconciler non ha nulla su cui agire.
    _entry("like_reconcile_interval_seconds", "int", 60, GROUP_RECONCILIATION,
           "Intervallo riconciliazione like (s)",
           "Attesa prima del primo nuovo tentativo del 'mi piace' sul sync provider finché la traccia non è indicizzata; raddoppia a ogni tentativo, fino a un'ora.",
           minimum=15, maximum=3600, step=5, requires=(requires_setting("sync_provider"),)),
    _entry("like_reconcile_max_age_hours", "int", 6, GROUP_RECONCILIATION,
           "Scadenza riconciliazione like (ore)",
           "Oltre questa età un 'mi piace' non ancora propagato al sync provider viene abbandonato (la traccia non è mai comparsa nel sync).",
           minimum=1, maximum=168, step=1, requires=(requires_setting("sync_provider"),)),
    _entry("delegation_max_age_hours", "int", 12, GROUP_RECONCILIATION,
           "Max-age delega (h)",
           "Ore di osservazione reale (poll a cui il sistema esterno ha risposto) senza esito, oltre le quali un download delegato è marcato come fallito. I poll saltati o senza risposta non contano.",
           minimum=1, maximum=168, step=1),
    _entry("delegation_max_wait_hours", "int", 72, GROUP_RECONCILIATION,
           "Attesa massima delega (h)",
           "Oltre questo tempo dal dispaccio, anche senza abbastanza poll con risposta (sistema esterno irraggiungibile, poll saltati), un download delegato è chiuso come fallito ma resta ritentabile.",
           minimum=1, maximum=720, step=1),
    # L'intervallo dello sweep periodico (workers.maintenance.sweep_delegations)
    # e' ora un routine (vedi GROUP_ROUTINES / core/routines.py:
    # routine.sweep_delegations.*), dinamico — non piu' letto una volta sola
    # all'avvio di beat.
    _entry("delegation_webhook_token", "string", "", GROUP_RECONCILIATION,
           "Token webhook", "Token segreto che i sistemi esterni includono per autenticare le notifiche push.",
           secret=True, required=False),
    _entry("public_base_url", "string", "", GROUP_RECONCILIATION,
           "URL pubblico musizeer",
           "URL con cui i sistemi esterni raggiungono musizeer (es. http://100.x.y.z:8000), per auto-registrare i webhook. Deve essere raggiungibile DA quel sistema, non dal browser: nella stessa rete Docker usa un URL interno (es. http://frontend).",
           required=False),
) + _download_request_settings() + _routine_settings()


# Indice per lookup O(1).
_BY_KEY: dict[str, SettingDef] = {s.key: s for s in CATALOG}


def get_def(key: str) -> SettingDef | None:
    return _BY_KEY.get(key)


def provider_order_settings() -> tuple[SettingDef, ...]:
    """The settings that ORDER the providers of a category: every ``list`` that declares a
    ``provider_category``. Their stored value is a preference (empty, partial or out of date);
    what readers use is the effective order (``services/provider_order.py``). Driven by the
    declaration, never by a key name."""
    return tuple(s for s in CATALOG if s.type == "list" and s.provider_category)


def catalog_defaults() -> dict[str, Any]:
    """{key: default} — i seed usati da ConfigManager. Unica sorgente dei default."""
    return {s.key: s.default for s in CATALOG}


def fail_safe_defaults() -> dict[str, Any]:
    """{key: fail_safe} — solo le voci che dichiarano un ``fail_safe``. Letto
    da ``ConfigManager`` per la finestra "store mai letto con successo"."""
    return {s.key: s.fail_safe for s in CATALOG if s.fail_safe is not _NO_FAIL_SAFE}


def public_taxonomy() -> dict[str, Any]:
    """Metadati dichiarativi di aree e gruppi (label, descrizione, area di
    appartenenza), nell'ORDINE ESPLICITO di visualizzazione: aree secondo
    AREA_ORDER, gruppi raggruppati per area secondo GROUPS. Il frontend legge
    questo invece di tenere una propria mappa hardcoded (stesso principio
    delle label di provider, già lette da GET /providers).

    I gruppi di provider (uno per provider registrato) non compaiono qui:
    appartengono sempre all'area "providers" (vedi GROUP_AREAS) ma la loro
    label è quella del provider stesso, esposta da GET /providers — mai
    duplicata qui, che significherebbe cablare un nome di provider nel core.
    """
    return {
        # label_key: chiave stabile con cui la UI traduce; label resta il
        # testo di default.
        "areas": [
            {"key": a, "label_key": f"settings.areas.{a}", "label": AREA_LABELS[a]}
            for a in AREA_ORDER
        ],
        "groups": [
            {
                "key": g.key, "label_key": f"settings.groups.{g.key}", "label": g.label,
                "description_key": f"settings.group_descriptions.{g.key}", "description": g.description,
                "area": g.area,
            }
            for g in GROUPS
        ],
    }
