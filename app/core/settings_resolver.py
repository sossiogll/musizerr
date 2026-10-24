"""
Motore di risoluzione delle impostazioni: risponde a "quali impostazioni
esistono, qual è il loro valore effettivo, e chi può leggere/scrivere
ciascuna", con la coercizione di tipo e la validazione dichiarate.

PURA LOGICA: nessuna route, nessuno storage, nessun formato di config.
Provider-agnostico: nessun nome di provider concreto compare qui. Il registry
è preso come dipendenza iniettata, tipizzata sul Protocol ``ProviderRegistry``
di ``core/interfaces.py`` (stesso pattern di ``SearchAggregator`` e
``DownloadResolver``) — così questo modulo non importa mai da ``providers/``.

Governance di lettura/scrittura per scope (vedi ``SettingScope`` in
``settings_catalog.py``):

    scope                       | admin legge | admin scrive         | non-admin legge | non-admin scrive
    ----------------------------|-------------|----------------------|------------------|------------------
    system                      | sì          | sì                   | NO               | no
    user                        | sì (proprio)| sì (proprio)         | sì (proprio)     | sì (proprio)
    system_with_user_override   | sì          | sì (default globale) | sì               | sì (proprio override)

Il diniego di lettura ai non-admin sullo scope 'system' è una correzione di
sicurezza deliberata: oggi ogni utente autenticato può leggere ogni
impostazione admin, incluse quelle che espongono un indirizzo di rete interno
e un path del filesystem del server. Nessun percorso frontend non-admin
dipende da un valore system-scoped, quindi negarlo è sicuro.
"""

from __future__ import annotations

from typing import Any, Callable

from musizeer_sdk.settings import ProviderSetting

from . import folders
from .interfaces import ProviderRegistry
from .settings_catalog import AREA_PROVIDERS, CATALOG, SettingDef

# ---------------------------------------------------------------------------
# Namespacing delle chiavi
# ---------------------------------------------------------------------------

# Le impostazioni di provider e quelle d'app vivono in un unico keyspace
# piatto senza collisioni: più provider dichiarano legittimamente la stessa
# short key (es. "search_limit", "url"). Lo storage lavora SEMPRE sulla short
# key (mai da rinominare su disco): il namespace esiste solo a questo livello.
_PROVIDER_KEY_PREFIX = "provider."


def namespace_provider_key(provider_name: str, short_key: str) -> str:
    """Costruisce la chiave namespaced ``provider.<provider_name>.<short_key>``
    a partire da un nome di provider e la sua short key."""
    return f"{_PROVIDER_KEY_PREFIX}{provider_name}.{short_key}"


def split_provider_key(key: str) -> tuple[str | None, str]:
    """Inversa di ``namespace_provider_key``: (provider_name, short_key) per
    una chiave namespaced, oppure (None, key) per una chiave app-level.

    Robusta a una short key che contiene a sua volta un punto: dopo il
    prefisso si spacca solo sul PRIMO punto (il nome del provider non ne
    contiene), il resto — punti inclusi — resta nella short key. Round trip
    con ``namespace_provider_key`` sempre esatto."""
    if not key.startswith(_PROVIDER_KEY_PREFIX):
        return None, key
    rest = key[len(_PROVIDER_KEY_PREFIX):]
    provider_name, sep, short_key = rest.partition(".")
    if not sep:
        # "provider.xxx" senza un secondo punto: non è una vera chiave
        # namespaced (un nome di provider da solo non è mai valido come
        # short key implicita) — trattala come chiave app-level letterale.
        return None, key
    return provider_name, short_key


def provider_config_from_system_settings(provider_name: str, system_settings: dict[str, Any]) -> dict[str, Any]:
    """Ricostruisce ``{short_key: value}`` per ``provider_name`` a partire da
    un dict di righe system-scope già lette (tipicamente
    ``SettingsRepository.all_system()``), filtrando per prefisso namespaced.

    PURA LOGICA (nessuno storage: prende un dict già in memoria, non una
    ``Session``) — l'unico punto che traduce fra il namespace piatto della
    tabella ``settings`` e la config SHORT-key che un provider si aspetta.
    Condivisa da ``SettingsService.provider_config`` (services/settings.py)
    e da ``HostProviderContext`` (services/provider_context.py, l'implementazione
    che ogni provider consulta tramite ``self.context``), cosi' la traduzione
    vive in un solo posto invece di essere ricopiata a ogni chiamante."""
    result: dict[str, Any] = {}
    for key, value in system_settings.items():
        p_name, short_key = split_provider_key(key)
        if p_name == provider_name:
            result[short_key] = value
    return result


# ---------------------------------------------------------------------------
# Schema unificato
# ---------------------------------------------------------------------------

# Ordine stabile di attraversamento delle categorie di provider — un tuple di
# nomi di metodo del Protocol ProviderRegistry, non una lista di provider:
# nessun nome di provider concreto compare qui. Pubblica (non prefissata da
# `_`): è anche la fonte unica da cui GET /providers (routes/config.py)
# deriva le categorie della sua risposta, cosi' le due liste non possono
# disallinearsi.
PROVIDER_CATEGORY_GETTERS: tuple[str, ...] = (
    "all_metadata",
    "all_download",
    "all_lyrics",
    "all_storage",
    "all_sync",
    "all_identification",
)


def find_provider_by_name(registry: ProviderRegistry, name: str):
    """Cerca ``name`` in TUTTE le categorie che il registry dichiara
    (``PROVIDER_CATEGORY_GETTERS``), mai in un sottoinsieme scritto a mano —
    un elenco hardcoded di categorie è esattamente ciò che ha fatto sparire
    "identification" tre volte (una in ``routes/config.py::_find_provider``,
    già corretta cosi'; una in ``routes/auth.py::_get_provider``, corretta
    qui; l'ordine di scoperta non importa quindi cambiare
    ``PROVIDER_CATEGORY_GETTERS`` qui li corregge entrambi in un colpo solo).
    Ritorna ``None`` se non trovato in nessuna categoria — il chiamante
    decide come reagire (es. un 404 HTTP), questa funzione resta PURA
    LOGICA (nessuna eccezione HTTP qui, vedi CLAUDE.md)."""
    for getter_name in PROVIDER_CATEGORY_GETTERS:
        category = getter_name.removeprefix("all_")
        provider = getattr(registry, f"get_{category}_provider")(name)
        if provider:
            return provider
    return None


def provider_setting_def(provider_name: str, setting: ProviderSetting) -> SettingDef:
    """La impostazione che un provider dichiara (``BaseProvider.config_schema``)
    come voce dello schema dell'app. Il provider dichiara solo i dati di
    ``ProviderSetting``; tutto il resto lo decide l'host, mai il provider:
    la chiave nel namespace del provider (e con essa le chiavi a cui rimanda
    ``visible_when``, che per il provider sono le sue short key), il gruppo (il nome del provider
    stesso, che per la UI è già etichettato da ``GET /providers``), l'area
    ("providers") e la governance (scope di sistema, non nascosta, senza
    requisiti)."""
    is_folder = setting.type == "folder"
    return SettingDef(
        key=namespace_provider_key(provider_name, setting.key),
        type=setting.type,
        # A folder has no default root (ADR-0008): what the provider declares is only the path to SUGGEST inside the root
        # the administrator picks, and an unset folder is unset.
        default=None if is_folder else setting.default,
        suggested_path=setting.default if is_folder else None,
        group=provider_name,
        label=setting.label,
        description=setting.description,
        area=AREA_PROVIDERS,
        options=setting.options,
        minimum=setting.minimum,
        maximum=setting.maximum,
        step=setting.step,
        secret=setting.secret,
        visible_when=(
            tuple((namespace_provider_key(provider_name, key), value) for key, value in setting.visible_when)
            if setting.visible_when else setting.visible_when
        ),
        required=setting.required,
    )


def merged_schema(registry: ProviderRegistry) -> list[SettingDef]:
    """
    Elenco COMPLETO di ``SettingDef`` per l'intera applicazione: prima ogni
    voce del CATALOG core (invariata), poi le impostazioni di ogni provider
    registrato, ri-chiavate nel namespace di cui sopra. Ordine stabile e
    deterministico: CATALOG prima, poi le categorie nell'ordine dichiarato
    sopra, poi l'ordine di ``config_schema()`` del singolo provider.

    Un provider può comparire in più di una categoria — es. un provider duale
    sync+metadata cross-registrato dal ProviderRegistry — e va contato una
    sola volta: dedup per IDENTITÀ dell'oggetto provider, non iterando le
    categorie alla cieca (altrimenti le sue impostazioni comparirebbero due
    volte). In aggiunta, se due provider DIVERSI condividono deliberatamente
    lo stesso nome (e quindi lo stesso storage di config) attraverso due
    categorie diverse, la chiave namespaced risultante può combaciare: in tal
    caso vince la prima dichiarazione incontrata nell'ordine stabile — mai
    una collisione silenziosa a chiave duplicata nell'elenco finale.
    """
    result: list[SettingDef] = list(CATALOG)
    seen_provider_ids: set[int] = set()
    seen_keys: set[str] = {s.key for s in CATALOG}

    for getter_name in PROVIDER_CATEGORY_GETTERS:
        providers = getattr(registry, getter_name)()
        for provider in providers:
            if id(provider) in seen_provider_ids:
                continue
            seen_provider_ids.add(id(provider))

            for declared in provider.config_schema():
                # Area, gruppo e chiave namespaced sono decisi qui, mai dal
                # provider (vedi ``provider_setting_def``): un provider non
                # deve MAI doversi ricordare di dichiarare la propria area.
                setting = provider_setting_def(provider.name, declared)
                if setting.key in seen_keys:
                    continue
                seen_keys.add(setting.key)
                result.append(setting)

    return result


# ---------------------------------------------------------------------------
# Policy di lettura/scrittura
# ---------------------------------------------------------------------------

_ADMIN_ROLE = "admin"


def can_read(setting: SettingDef, role: str) -> bool:
    """True se ``role`` può leggere questa impostazione. Guidato ESCLUSIVAMENTE
    da ``setting.scope``, mai dal nome della chiave. Vedi la tabella in cima
    al modulo: l'unico scope che nega la lettura a un ruolo non-admin è
    'system' (fix di sicurezza deliberato)."""
    if setting.scope == "system":
        return role == _ADMIN_ROLE
    return True


def can_write(setting: SettingDef, role: str) -> bool:
    """True se ``role`` può scrivere questa impostazione. Stessa governance
    della lettura: 'system' è riservato all'admin. Per 'user' e
    'system_with_user_override' entrambi i ruoli possono scrivere — cambia
    solo QUALE valore viene scritto (il proprio per un non-admin, il default
    globale per un admin su 'system_with_user_override'), una distinzione
    che appartiene al layer HTTP e non a questo predicato."""
    if setting.scope == "system":
        return role == _ADMIN_ROLE
    return True


# ---------------------------------------------------------------------------
# Errore dedicato (coercizione e validazione)
# ---------------------------------------------------------------------------


class SettingError(Exception):
    """Sollevato da ``coerce_value``/``validate_value``. Porta sempre la
    chiave incriminata e un messaggio mostrabile direttamente in una UI. Mai
    una sostituzione silenziosa col default."""

    def __init__(self, key: str, message: str, error_code: str | None = None) -> None:
        self.key = key
        self.message = message
        # A stable code the interface translates, when the refusal has one (a folder).
        self.error_code = error_code
        super().__init__(f"{key}: {message}")


# ---------------------------------------------------------------------------
# Coercizione
# ---------------------------------------------------------------------------

_TRUE_STRINGS = {"true", "1", "yes", "on"}
_FALSE_STRINGS = {"false", "0", "no", "off"}


def _coerce_bool(setting: SettingDef, raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    raise SettingError(setting.key, f"'{raw}' non è un valore booleano valido")


def _coerce_number(setting: SettingDef, raw: Any) -> int | float:
    # bool è sottoclasse di int in Python: un booleano non è mai un numero
    # legittimo per un campo int/float, va rifiutato esplicitamente.
    if isinstance(raw, bool):
        raise SettingError(setting.key, f"un booleano non è un valore numerico valido per '{setting.key}'")

    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw)
        except ValueError:
            raise SettingError(setting.key, f"'{raw}' non è un numero valido per '{setting.key}'") from None
    else:
        raise SettingError(setting.key, f"tipo non numerico per '{setting.key}': {raw!r}")

    if setting.type == "int":
        if not value.is_integer():
            raise SettingError(setting.key, f"'{raw}' non è un intero valido per '{setting.key}'")
        return int(value)
    return value


def _coerce_string(setting: SettingDef, raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (int, float, bool)):
        return str(raw)
    raise SettingError(setting.key, f"valore non convertibile in stringa per '{setting.key}': {raw!r}")


def _coerce_string_like(setting: SettingDef, raw: Any) -> str:
    """Per enum/provider_ref: SOLO una stringa (mai coercita da uno scalare
    diverso — a differenza di 'string', qui il valore identifica un'opzione o
    un nome di provider, non un testo libero)."""
    if isinstance(raw, str):
        return raw
    raise SettingError(setting.key, f"valore non stringa per '{setting.key}': {raw!r}")


def _coerce_list(setting: SettingDef, raw: Any) -> list:
    if isinstance(raw, list):
        return list(raw)
    raise SettingError(setting.key, f"valore non lista per '{setting.key}': {raw!r}")


def _coerce_folder(setting: SettingDef, raw: Any) -> dict[str, str] | None:
    """A ``folder`` is ``{"root_id", "path"}`` (a root folder and a relative path inside it, ``core/folders.py``); an empty
    value (``""``, ``{}``, ``None``) is "no folder". Whether the root exists is the host's to check
    (``services/settings.py``): this module reads no database."""
    if folders.is_unset(raw):
        return None
    try:
        return folders.parse_value(raw).stored()
    except folders.FolderError as refused:
        raise SettingError(setting.key, refused.message, refused.code) from refused


def coerce_value(setting: SettingDef, raw: Any) -> Any:
    """Converte un valore RAW in arrivo (es. dal body JSON di una richiesta)
    nel tipo dichiarato da ``setting.type``. Non sostituisce mai silenziosamente
    un default: ogni valore non coercibile solleva ``SettingError``."""
    if setting.type == "bool":
        return _coerce_bool(setting, raw)
    if setting.type in ("int", "float"):
        return _coerce_number(setting, raw)
    if setting.type == "string":
        return _coerce_string(setting, raw)
    if setting.type in ("enum", "provider_ref"):
        return _coerce_string_like(setting, raw)
    if setting.type == "list":
        return _coerce_list(setting, raw)
    if setting.type == "folder":
        return _coerce_folder(setting, raw)
    raise SettingError(setting.key, f"tipo di impostazione sconosciuto: '{setting.type}'")


# ---------------------------------------------------------------------------
# Validazione
# ---------------------------------------------------------------------------


def is_empty_value(value: Any) -> bool:
    """0, False e [] con elementi NON sono 'vuoti': solo None, stringa vuota e
    lista/tupla vuota lo sono. Un booleano False o un numero 0 sono valori
    configurati legittimi, non l'assenza di un valore.

    Pubblica (non ``_is_empty``): serve anche a ``evaluate_requirement`` qui
    sotto e a ``services.settings`` per il requisito "setting_set" e per
    il confronto "e' il default?" in scrittura — vedi CLAUDE.md, "non creare
    funzioni private in core/ che servono altrove"."""
    if value is None:
        return True
    if isinstance(value, str) and value == "":
        return True
    if isinstance(value, (list, tuple)) and len(value) == 0:
        return True
    return False


def validate_value(setting: SettingDef, value: Any, registry: ProviderRegistry) -> None:
    """Verifica un valore GIÀ coerciso contro la dichiarazione. Solleva
    ``SettingError`` al primo vincolo violato.

    Un valore assente (vedi ``_is_empty``) su un'impostazione NON required è
    sempre legittimo — non solo per ``provider_ref`` (un provider_ref
    opzionale esiste già oggi nel catalogo reale) ma per qualunque tipo con
    vincoli aggiuntivi (es. un enum opzionale non ancora impostato): un
    valore mancante non viola un vincolo su un valore che non c'è. Solo un
    valore REALMENTE presente viene controllato contro options/minimum/
    maximum/provider_category."""
    if is_empty_value(value):
        if setting.required:
            raise SettingError(setting.key, f"'{setting.key}' è obbligatorio e non può essere vuoto")
        return

    if setting.type == "provider_ref":
        valid_names = registry.names_for_category(setting.provider_category) if setting.provider_category else []
        if value not in valid_names:
            raise SettingError(
                setting.key,
                f"'{value}' non è un provider valido per la categoria '{setting.provider_category}'",
            )
        return

    if setting.type == "enum":
        if setting.options and value not in setting.options:
            raise SettingError(
                setting.key,
                f"'{value}' non è tra le opzioni ammesse per '{setting.key}': {list(setting.options)}",
            )
        return

    if setting.type in ("int", "float"):
        if setting.minimum is not None and value < setting.minimum:
            raise SettingError(setting.key, f"'{value}' è sotto il minimo consentito ({setting.minimum})")
        if setting.maximum is not None and value > setting.maximum:
            raise SettingError(setting.key, f"'{value}' supera il massimo consentito ({setting.maximum})")


# ---------------------------------------------------------------------------
# "usable" — UNICA definizione, condivisa da GET /providers (routes/config.py)
# e dalla valutazione dei requisiti delle impostazioni.
# ---------------------------------------------------------------------------


def compute_provider_usable(provider, *, authenticated: bool, config_valid: bool) -> bool:
    """Un provider è "usable" quando è autenticato (o non richiede login,
    ``auth.flow == NONE``) E la sua configurazione è valida
    (``validate_config``) E, per i provider il cui contratto espone un check
    di disponibilità aggiuntivo (``IdentificationProvider.is_available()``,
    es. libreria/dipendenza esterna presente), quel check passa.

    Non chiama nulla da sola (nessuna Session/registry qui: PURA LOGICA) —
    ``authenticated``/``config_valid`` sono calcolati dal chiamante, che ha il
    contesto (credential store, sessione) per farlo. Questa è l'unica
    definizione: sia ``routes.config._provider_info`` (GET /providers) sia
    ``SettingsService.provider_is_usable`` (requisito "provider_category_usable")
    la richiamano invece di ridefinirla — vedi CLAUDE.md."""
    usable = authenticated and config_valid
    if "identification" in provider.roles:
        usable = usable and provider.is_available()
    return usable


# ---------------------------------------------------------------------------
# Requisiti di disponibilità (Requirement) — valutazione pura.
# ---------------------------------------------------------------------------


def evaluate_requirement(
    req,
    *,
    effective_values: dict[str, Any],
    schema_by_key: dict[str, SettingDef],
    category_usable: Callable[[str], bool],
) -> tuple[bool, str | None]:
    """Valuta UN requisito dichiarato. Ritorna (soddisfatto, motivo_se_no).

    Il motivo è costruito SOLO da dati dichiarati (la categoria del
    requisito, la label dell'altra impostazione) — mai da un nome di
    provider hardcoded, come richiesto dalla Regola d'Oro."""
    if req.kind == "provider_category_usable":
        if category_usable(req.provider_category):
            return True, None
        return False, f"requires a usable '{req.provider_category}' provider"

    if req.kind == "setting_set":
        other = schema_by_key.get(req.setting_key)
        value = effective_values.get(req.setting_key)
        if not is_empty_value(value):
            return True, None
        label = other.label if other is not None else req.setting_key
        return False, f"requires '{label}' to be set"

    raise ValueError(f"tipo di requisito sconosciuto: {req.kind!r}")


def setting_availability(
    setting: SettingDef,
    *,
    effective_values: dict[str, Any],
    schema_by_key: dict[str, SettingDef],
    category_usable: Callable[[str], bool],
) -> tuple[bool, str | None]:
    """Valuta TUTTI i requisiti di ``setting`` contro lo stato dato. Ritorna
    (disponibile, motivo_se_no) — più motivi inevasi sono uniti con '; '."""
    reasons: list[str] = []
    for req in setting.requires:
        ok, reason = evaluate_requirement(
            req,
            effective_values=effective_values,
            schema_by_key=schema_by_key,
            category_usable=category_usable,
        )
        if not ok:
            reasons.append(reason)
    if reasons:
        return False, "; ".join(reasons)
    return True, None
