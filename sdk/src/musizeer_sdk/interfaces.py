"""
The provider contract: the abstract base class of each kind of provider and
the data types that cross the boundary with them. A provider subclasses one of
these and imports nothing else of Musizeer; the host (or, later, a runner in
another process) calls the methods.

Every optional method has a default (empty result, ``None`` or
``NotImplementedError``); a capability is DECLARED with a ``supports_*``
method, never inferred from which methods a class overrides.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .auth import AccountIdentity, AuthDefinition, AuthSession
from .context import ProviderContext
from .delegation import DelegationStatus, FileRef
from .errors import DelegateHoldsRelease
from .identifiers import PROVIDER_ID, Identifier
from .models import Album, Artist, LibraryItem, Lyrics, Playlist, Track
from .permissions import NO_PERMISSIONS, Permissions
from .settings import ProviderSetting

# Versione del contratto dei provider. Un pacchetto installato da fuori
# dichiara in `provider_api_version` quella che implementa; il registry
# rifiuta un pacchetto che ne dichiara un'altra (o nessuna).
PROVIDER_API_VERSION = 6


class BaseProvider(ABC):
    name: str
    label: str
    auth: AuthDefinition
    # What the sandbox grants this provider (see ``musizeer_sdk.permissions``): nothing
    # unless it declares it.
    permissions: Permissions = NO_PERMISSIONS

    def __init__(self, context: ProviderContext) -> None:
        # Unico canale verso l'host oltre AuthSession e i valori di ritorno
        # (vedi ProviderContext) — un provider non importa mai
        # ConfigManager o una variabile d'ambiente per parlare con Musizeer.
        self.context = context

    def config_schema(self) -> list[ProviderSetting]:
        """Dichiarazione della config del provider: le impostazioni che
        l'utente può cambiare, lette poi con ``self.context.setting(key)``.
        L'host le espone sotto il nome del provider; il provider non dichiara
        dove vivono né chi le scrive. Default: nessuna."""
        return []

    @abstractmethod
    def validate_config(self, config: dict) -> bool:
        """Verifica che la config fornita sia valida prima di salvare."""

    def is_library_available(self) -> bool | None:
        """Se la libreria Python esterna da cui il provider dipende (vedi
        ``AuthDefinition.library_package``) è installata; None se non
        dipende da nessuna."""
        return None

    @abstractmethod
    def test_connection(self, session: AuthSession) -> str | None:
        """Test connectivity. Returns None on success, error message on failure."""

    def supports_login(self) -> bool:
        """Whether a user may sign in to Musizeer through their own account of
        this provider (``identify_account`` is implemented). Declared, never
        inferred. Only meaningful for a provider with personal accounts
        (``AuthDefinition.account_model``): the host ignores it for any other."""
        return False

    def identify_account(self, session: AuthSession) -> AccountIdentity | None:
        """Optional (``supports_login``). Who the account behind ``session`` is
        on THIS provider: its stable id, which the host stores with the linked
        account and signs the user in by. ``session`` is the one the host built
        for the account: the OAuth2 token the user just granted, or, for a
        login account, the submitted credentials in ``session.extra`` (this
        call is what checks them). None = the provider does not know this
        account (credentials refused); raise when the service cannot be reached
        (the host answers "try later"). Never an email: an address is not an
        identity."""
        return None

    def get_cover_url(self, cover_id: str, session: AuthSession) -> str | None:
        """
        Risolve un cover_id interno (quello restituito da questo stesso provider
        nel campo cover_url) in un URL direttamente fetchable.
        Override opzionale: serve solo ai provider il cui cover_url non e'
        raggiungibile direttamente dal browser (es. host interno con auth),
        nel qual caso cover_url è ``self.context.cover_url(cover_id)``: l'host
        inoltra la cover chiedendo l'URL reale a questo metodo.
        """
        return None


class MetadataProvider(BaseProvider, ABC):
    # True when the ``stream_url`` this provider sets on its tracks is the
    # FULL track (freely streamable without downloading), not a preview.
    # The host then offers full playback from the provider (``playback.source
    # == "provider"``); otherwise a provider ``stream_url`` is not trusted as
    # full audio.
    streams_full_tracks: bool = False

    def search_tracks(self, query: str, session: AuthSession, limit: int = 20) -> list[Track]:
        return []

    def search_albums(self, query: str, session: AuthSession, limit: int = 20) -> list[Album]:
        return []

    def search_artists(self, query: str, session: AuthSession, limit: int = 20) -> list[Artist]:
        return []

    def search_playlists(self, query: str, session: AuthSession, limit: int = 20) -> list[Playlist]:
        return []

    def get_track(self, provider_id: str, session: AuthSession) -> Track | None:
        return None

    def get_preview_url(self, provider_id: str, session: AuthSession) -> str | None:
        """
        L'URL dell'anteprima di un proprio brano, valido ORA. Override
        opzionale, per i provider il cui URL di anteprima è firmato e scade:
        sui brani mettono ``preview_url = self.context.preview_url(id)`` e
        l'host, a ogni ascolto, chiede qui l'URL aggiornato e lo inoltra (come
        per le cover, ``get_cover_url``). Così nessuna cache (dell'host o del
        browser) conserva un URL scaduto.
        """
        return None

    def supports_isrc_lookup(self) -> bool:
        """Whether ``find_by_isrc`` is implemented: the provider's service can
        look a recording up by its ISRC. Declared, never inferred from the
        methods overridden: the host asks the providers that declare it before
        it falls back to a text search."""
        return False

    def find_by_isrc(self, isrc: str, session: AuthSession) -> Track | None:
        """Lookup diretto per ISRC (``supports_isrc_lookup``). Override
        opzionale."""
        return None

    def find_related_tracks(self, track: Track, session: AuthSession, limit: int = 20) -> list[Track]:
        """Trova varianti correlate della stessa traccia nello stesso provider.
        *track* può venire da un altro provider: il proprio id, se c'è, è fra
        i suoi ``external_ids`` (``get_id("provider_id", self.name)``), e il
        suo ``provider_id`` è quello dell'altro provider, da non usare mai
        qui. Override opzionale."""
        return []

    def find_similar_tracks(self, track: Track, session: AuthSession, limit: int = 20) -> list[Track]:
        """Altre tracce (registrazioni diverse) che piacerebbero a chi ascolta
        *track*: radio dell'artista, raccomandazioni, somiglianza di genere.
        *track* può venire da un altro provider: il proprio id, se c'è, è fra
        i suoi ``external_ids`` (``get_id("provider_id", self.name)``). Al
        massimo ``limit``, nell'ordine del provider. Override opzionale: solo
        i provider il cui servizio ha un endpoint del genere."""
        return []

    def get_album(self, provider_id: str, session: AuthSession) -> Album | None:
        return None

    def get_artist(self, provider_id: str, session: AuthSession) -> Artist | None:
        return None

    def get_artist_albums(self, provider_id: str, session: AuthSession, limit: int = 50) -> list[Album]:
        return []

    def supports_discography(self) -> bool:
        """Whether the provider is a catalog that lists the releases of an
        artist (``get_artist_albums``) AND gives the tracks of each of them
        (``get_album``): what the host needs to gather a whole discography
        (``services/discography.py``). Declared, never inferred from the
        methods overridden: a provider that lists a library's own files
        (a sync provider) answers False."""
        return False

    def get_artist_top_tracks(self, provider_id: str, session: AuthSession, limit: int = 10) -> list[Track]:
        """Le tracce più popolari dell'artista, nell'ordine del provider, al
        massimo ``limit``. Override opzionale: solo i provider il cui servizio
        ne conosce la popolarità."""
        return []

    def get_playlist(self, provider_id: str, session: AuthSession) -> Playlist | None:
        return None

    def get_playlist_snapshot(self, provider_id: str, session: AuthSession) -> str | None:
        """Optional. The opaque version marker of the playlist's content (a
        snapshot id, an etag) WITHOUT reading its tracks: the host stores it
        for a followed playlist and reads the tracks again only when it
        changes. None = the provider has no cheap marker: the host reads the
        playlist (``get_playlist``) and compares ``Playlist.snapshot_id``, or,
        when that is empty too, the tracks themselves."""
        return None

    def restricts_playlist_tracks(self) -> bool:
        """Whether the service hands out the tracks of a playlist only for the
        ones the connected account owns or collaborates on: a playlist of
        another owner may then come back with ``tracks_unavailable``. Declared,
        never inferred (``CAPABILITY_PLAYLIST_TRACKS_RESTRICTED``)."""
        return False

    # ------------------------------------------------------------------
    # Discovery — "trending" is provider-defined (chart, popularity_week, etc.)
    # ------------------------------------------------------------------

    def get_new_releases(self, session: AuthSession, limit: int = 20) -> list[Album]:
        return []

    def get_trending_albums(self, session: AuthSession, limit: int = 20) -> list[Album]:
        return []

    def get_trending_tracks(self, session: AuthSession, limit: int = 20) -> list[Track]:
        return []

    def get_trending_artists(self, session: AuthSession, limit: int = 20) -> list[Artist]:
        return []

    # ------------------------------------------------------------------
    # User library (requires user-level auth)
    # ------------------------------------------------------------------

    def get_saved_tracks(self, session: AuthSession, limit: int = 50, offset: int = 0) -> list[Track]:
        return []

    def iter_saved_tracks(self, session: AuthSession, page_size: int = 50) -> Iterator[list[Track]]:
        """The user's saved (liked) tracks, one list per page, in the
        provider's order: how the host imports the whole library
        (the host's likes import), reading each page ONCE. The default pages
        through ``get_saved_tracks`` by offset and ends at an empty or short
        page. Override it when the service pages by cursor or costs quota per
        call (a provider that must keep a cursor between pages, or that drops
        part of a page after reading it, so that a short page does not mean
        the last): the provider decides where the library ends, the host never
        infers it. An empty list is a page with nothing to keep, not the end."""
        offset = 0
        while True:
            batch = self.get_saved_tracks(session, limit=page_size, offset=offset)
            if not batch:
                return
            yield batch
            if len(batch) < page_size:
                return
            offset += page_size

    def get_saved_albums(self, session: AuthSession, limit: int = 50, offset: int = 0) -> list[Album]:
        return []

    def get_followed_artists(self, session: AuthSession, limit: int = 50, offset: int = 0) -> list[Artist]:
        return []

    def get_saved_playlists(self, session: AuthSession, limit: int = 50, offset: int = 0) -> list[Playlist]:
        return []

    def supports_user_library(self) -> bool:
        return False

    def supports_unlike_saved_track(self) -> bool:
        """Whether ``unlike_saved_track`` is implemented: the provider's API
        lets an app take a track off the user's saved tracks. Declared, never
        inferred."""
        return False

    def unlike_saved_track(self, provider_id: str, session: AuthSession) -> None:
        """Optional. Takes the track ``provider_id`` off the signed-in user's
        saved (liked) tracks on THIS provider: the like that
        ``get_saved_tracks`` imported. Only where the API allows it
        (``supports_unlike_saved_track``); the host calls it when a user takes
        a favorite off and THAT user's setting ``unlike_on_origin_providers`` is
        on, always with a session of the user's own account (never a shared or
        app-only one), so the like does not come back with the next import.
        Raise ``PermissionError`` when the account did not grant the permission
        (the user is told to reconnect; declare the scope in
        ``OAuth2Config.capability_scopes``), anything else on another failure
        (the host retries nothing: it is best effort and the removal is
        remembered by the host either way)."""
        raise NotImplementedError



class LyricsProvider(BaseProvider, ABC):

    @abstractmethod
    def get_lyrics(self, track: Track, session: AuthSession) -> Lyrics | None:
        """Cerca i lyrics per la traccia data."""


class DownloadProvider(BaseProvider, ABC):
    supported_identifier_types: set[str] = set()
    enabled_by_default: bool = True
    # Quanti download di questo provider possono girare insieme, fra tutti i
    # worker. None = nessun limite. Il provider dichiara solo il vincolo: lo
    # fa rispettare l'host prima di chiamare download_by_id/download_fuzzy.
    max_concurrent_downloads: int | None = None
    # Il provider sa cercare una traccia dai suoi metadati (``download_fuzzy``
    # sovrascritto con una ricerca vera). Dichiarato, mai dedotto: senza, la
    # fase fuzzy del resolver non lo prova e non lo conta come tentato.
    supports_fuzzy: bool = False

    def supports_identifier(self, identifier: Identifier) -> bool:
        """Verifica se il provider supporta il download tramite questo identifier."""
        if identifier.storage_type() not in self.supported_identifier_types:
            return False
        if identifier.type == PROVIDER_ID:
            return identifier.provider == self.name
        return True

    @abstractmethod
    def download_by_id(
        self,
        identifier: Identifier,
        workspace: str,
        session: AuthSession,
    ) -> str | None:
        """
        Scarica la traccia identificata da ``identifier`` dentro ``workspace``,
        la cartella vuota che l'host ha dato a questa chiamata (ADR-0003).
        Ritorna il nome del file scritto, relativo a ``workspace`` e con la
        propria estensione, o None se il download non è riuscito. Dove finirà
        il file in libreria lo decide l'host.
        """

    def download_fuzzy(self, track: Track, workspace: str, session: AuthSession) -> str | None:
        """
        Fallback fuzzy match per provider senza ID universale. Override
        opzionale, insieme a ``supports_fuzzy = True``; stesso contratto di
        ``download_by_id`` per ``workspace`` e per il valore di ritorno. Può
        sollevare ``DownloadDelegated`` (consegnato a un sistema esterno) o
        ``DownloadDeclined`` (rifiuto definitivo e motivato). Se il sistema esterno
        ha GIÀ il rilascio della traccia, può sollevare ``DelegateHoldsRelease``
        con ciò che ha (dati): cosa significhi per la traccia lo decide l'host.
        """
        return None

    def dispatch_album(self, album: Album, session: AuthSession) -> None:
        """
        Dispatcha un intero album al sistema esterno come unità.
        Override opzionale: solo provider che gestiscono download a livello album
        (non per singola traccia) devono implementarlo.
        Solleva DownloadDelegated se il sistema esterno accetta.
        """
        raise NotImplementedError

    def search_delegated(self, held: DelegateHoldsRelease, session: AuthSession) -> None:
        """Optional, with ``DelegateHoldsRelease``. The host found that the
        release ``held`` describes is in the external system but neither has
        the requested track nor is complete: ask the system to look for it
        (monitor it, start a search...). Raises ``DownloadDelegated`` when it
        accepts; anything else (it refused, it is unreachable) is a transient
        error for the host."""
        raise NotImplementedError

    # --- Riconciliazione dei download delegati (capability opzionali) ---
    # Default: nessuna fase esterna da attendere → il download resta fire-and-forget
    # (comportamento storico). Un provider che delega a un sistema esterno le
    # implementa per far apprendere a musizeer stato e file prodotti, in modo
    # generico (nessun tipo API-specifico attraversa il contratto).

    def poll_delegation(self, handle: str, session: AuthSession) -> DelegationStatus | None:
        """Stato corrente di un download delegato (mappato su download_states),
        a livello dell'intero pacchetto delegato (es. un album).

        Il giudizio sulla TRACCIA richiesta non è di questo metodo: un pacchetto
        incompleto può già contenere il file della traccia richiesta, ma a
        deciderlo è l'host, con i dati di ``list_delegated_tracks``.

        Tre esiti distinti, tutti legittimi:

        - Ritorna un ``DelegationStatus`` quando il sistema esterno ha
          risposto CON UN SEGNALE DI ATTIVITÀ REALE (es. l'item è ancora in
          coda/in lavorazione lato sistema esterno) → stato non terminale
          "genuinamente in corso": il chiamante applica lo stato e, non
          essendo terminale, valuta il max-age normalmente (il delegato ha
          parlato, quindi il tempo trascorso conta, anche se il verdetto
          finale sarà "ancora in corso").
        - Ritorna un ``DelegationStatus`` non terminale anche quando il
          sistema esterno ha risposto MA NON riporta alcun segnale di
          attività (nessun item in coda, nessun file corrispondente ancora
          prodotto) — è un ESITO REALE, non ignoranza: il chiamante lo
          tratta comunque come non terminale soggetto al max-age normale (che
          potrà trasformarlo in ``failed``), ma un'implementazione non deve
          MAI restituire uno stato che implica progresso attivo (es.
          ``downloading``) solo perché non ha trovato nulla — quello
          equivarrebbe a riportare ignoranza come se fosse progresso.
        - Ritorna ``None`` quando il provider non ha fasi asincrone da
          seguire (fire-and-forget by design) → il chiamante lo considera
          risolto e interrompe la riconciliazione.

        PUÒ SOLLEVARE un'eccezione quando il sistema esterno non ha risposto
        AFFATTO (rete irraggiungibile, timeout, credenziali non verificabili
        adesso) — "non determinabile ora", diverso sia da "risolto" sia da
        "fallito", e diverso dal caso sopra (il sistema HA risposto, solo
        senza segnali). Il chiamante generico
        (la riconciliazione dell'host) tratta l'eccezione
        come TRANSITORIA: ri-accoda senza applicare il max-age e senza
        finalizzare il record come failed, perché un'irraggiungibilità del
        sistema esterno non dice nulla sull'esito reale del download.
        """
        return None

    def list_delegated_tracks(self, handle: str, session: AuthSession) -> list[Track]:
        """Le tracce del pacchetto delegato (es. le tracce di un album) che
        hanno GIÀ un file presso il sistema esterno, senza decidere nulla: è
        l'host a stabilire, con il proprio matcher, se la traccia richiesta è
        fra queste e quindi già scaricata (anche se il resto del pacchetto è
        incompleto o fallito). Ogni ``Track`` porta ciò che il sistema sa di
        essa (titolo, artista, durata). ``album`` vuoto significa "il rilascio
        che è stato delegato": l'host lo riempie con l'album della traccia
        richiesta, perché il pacchetto è stato scelto per quella traccia e il
        titolo che il sistema esterno dà al rilascio può differire nella
        forma. Default: nessuna (l'host si affida a ``poll_delegation``).
        Come ``poll_delegation`` PUÒ SOLLEVARE se il sistema non ha
        risposto affatto: l'host lo tratta come transitorio."""
        return []

    def list_delegated_files(self, handle: str, session: AuthSession) -> list[FileRef]:
        """File prodotti dal sistema esterno per questo handle (path + qualità
        via API, senza byte). Default: non supportato."""
        return []

    def delete_file(self, ref: FileRef, session: AuthSession) -> bool:
        """Elimina un file gestito dal sistema esterno (risoluzione conflitti
        a livello API). Default: non supportato."""
        raise NotImplementedError

    def request_rescan(self, session: AuthSession) -> None:
        """Chiede al sistema esterno di rifare import/rename (es. dopo tag-repair).
        Default: no-op."""
        return None

    # --- Push da sistema esterno (capability opzionali) ---
    # Default: nessun supporto → il chiamante genericamente lo salta/ignora.
    # Nessun vocabolario di eventi/payload specifico di un sistema attraversa
    # il contratto: resta tutto dentro l'implementazione del provider.

    def delegation_event_handle(self, payload: dict) -> str | None:
        """
        Interpreta una notifica push RAW inviata da un sistema esterno e ne
        estrae l'handle di delega (lo stesso valore restituito come
        ``handle`` da ``DownloadDelegated``, confrontabile con
        ``Download.delegation_handle``). Ritorna None se il payload è
        irrilevante, malformato o non porta a nessuna azione.

        ATTENZIONE: ``payload`` è INPUT NON FIDATO — arriva da un sistema
        esterno raggiungibile in rete, senza garanzie sulla sua forma.
        Un'implementazione deve leggere esclusivamente campi strutturati
        attesi (es. un tipo di evento, un id numerico) e non deve MAI
        trattare alcun contenuto del payload come istruzione da eseguire.

        Default: nessun supporto → None.
        """
        return None

    def register_push_endpoint(self, callback_url: str, session: AuthSession) -> str | None:
        """
        Chiede al sistema esterno di chiamare ``callback_url`` sugli eventi
        rilevanti per la riconciliazione dei download delegati.

        What the answer means (the host never infers it from the return value
        alone):

        - A provider that does NOT support push does not override this method
          (the manifest's ``implements`` says so): the host reports
          "not supported" and never calls it.
        - A provider that overrides it returns a short outcome word
          ("created", "updated") when the external system accepted the
          registration.
        - When the external system refuses (a validation error because it
          cannot reach ``callback_url``, a rejected credential, an error of
          its own) or cannot be reached, the provider RAISES, with the
          external system's own reason in the message: the host classifies the
          failure (``unreachable``, ``auth_failed``, ``upstream_error``,
          ``failed``...) and shows the message to the administrator. An
          exception that carries ``response.status_code`` is classified by it.
          The provider must not put secrets in the message: ``callback_url``
          carries the shared token of the inbound webhook, so a message that
          quotes it must hide the token.
        - ``None`` from an implementation means only "nothing was registered
          and there is no reason to tell" (e.g. the system is not configured
          yet); the host reports it as a registration without an outcome, never
          as "not supported". Prefer raising with a reason.

        Default: nessun supporto → None (never called by the host).
        """
        return None


# ------------------------------------------------------------------
# Storage provider
# ------------------------------------------------------------------


@dataclass
class StorageStreamResult:
    """Risultato di StorageProvider.stream(): uno fra ``file`` (nome nella
    cartella di lavoro ricevuta), ``url`` (con eventuali ``headers``, che
    l'host inoltra) e ``chunks`` (blocchi di byte, per chi sa solo fare
    streaming). ``file`` permette all'host le richieste Range, quindi il
    seek nel player."""

    file: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    chunks: Iterator[bytes] | None = None
    media_type: str = "application/octet-stream"
    filename: str = ""
    # For ``chunks``: the size of the whole file, when known (the host sends
    # its length, and the player knows the duration), and the byte of the
    # file the chunks start from (``offset`` asked to ``stream``).
    size: int | None = None
    offset: int = 0


@dataclass(frozen=True)
class WorkspaceFile:
    """Un file dentro la cartella di lavoro che l'host ha dato a un provider
    per una singola operazione (ADR-0003): ``name`` è sempre relativo a
    ``workspace``."""

    workspace: str
    name: str

    @property
    def path(self) -> str:
        return str(Path(self.workspace) / self.name)


class StorageProvider(BaseProvider, ABC):
    """Gestisce DOVE i file vengono persistiti dopo il download."""

    # True when ``stream`` accepts ``offset`` (the bytes to skip) and hands
    # back ``chunks`` starting there with the file ``size``: the host then
    # answers a Range request (a seek in the player) with a 206. A provider
    # that returns a ``file`` or a ``url`` does not need it (the host serves
    # their ranges itself).
    supports_stream_offset: bool = False

    @abstractmethod
    def store(self, source: WorkspaceFile, relative_dest: str, session: AuthSession) -> str:
        """
        Persiste il file ``source`` (nella cartella di lavoro dell'host) in
        ``relative_dest``. Può spostarlo: l'host non lo rilegge dopo.
        Ritorna il path relativo finale (potrebbe differire da relative_dest
        se il provider rinomina o aggiusta).
        """

    @abstractmethod
    def exists(self, relative_path: str, session: AuthSession) -> bool:
        """Verifica se un file precedentemente salvato esiste ancora."""

    @abstractmethod
    def stream(self, relative_path: str, workspace: str, session: AuthSession) -> StorageStreamResult:
        """Rende disponibile il file salvato per lo streaming al client; un
        file va scritto (o collegato) dentro ``workspace``, che l'host
        rimuove a risposta finita. Un file remoto conviene renderlo come
        ``chunks`` man mano che arriva, non scaricarlo prima tutto: chi
        ascolta aspetterebbe l'intero trasferimento. Con
        ``supports_stream_offset`` riceve anche ``offset`` (keyword)."""

    @abstractmethod
    def delete(self, relative_path: str, session: AuthSession) -> bool:
        """Rimuove un file salvato. Ritorna True se eliminato."""

    # --- Capability opzionali (default: non supportata) ---

    def size(self, relative_path: str, session: AuthSession) -> int | None:
        """Dimensione in byte di un file salvato, o None se non determinabile/
        non supportato. Usata per la verifica cheap post-upload."""
        return None

    def list_files(self, prefix: str, session: AuthSession) -> Iterator[str] | None:
        """I path relativi (separatore ``/``, relativi alla radice della
        libreria, come quelli passati a ``store``) di ogni file salvato sotto
        ``prefix``; ``""`` è l'intera libreria. Elenca tutto, cartelle
        nascoste comprese: cosa ignorare lo decide l'host.

        ``None`` = il provider non sa elencare i propri file (default). Una
        libreria irraggiungibile solleva un'eccezione, così l'host la
        distingue da una libreria vuota."""
        return None


# ------------------------------------------------------------------
# Sync provider
# ------------------------------------------------------------------


@dataclass(frozen=True)
class SyncCandidate:
    """Una traccia della libreria di un sync provider che potrebbe essere quella
    cercata, col proprio id esplicito: ``sync_id`` è l'id con cui il provider
    la indirizza (``get_stream_url``, il like...), ``track`` ciò che ne sa
    (titolo, artisti, album, anno, durata, ISRC). Dato, non un verdetto: che sia
    la traccia cercata lo decide l'host."""

    sync_id: str
    track: Track


class SyncProvider(BaseProvider, ABC):
    """
    Provider opzionale che funge da source of truth per
    'questa traccia e disponibile nella libreria?'.
    Quando attivo, sovrascrive il check di default basato su DB.
    """

    @abstractmethod
    def trigger_scan(self, session: AuthSession) -> None:
        """Triggera un rescan della libreria dopo che nuovi file sono stati salvati."""

    @abstractmethod
    def find_candidates(self, track: Track, session: AuthSession) -> list[SyncCandidate]:
        """Le tracce della libreria sincronizzata che potrebbero essere ``track``,
        ciascuna col proprio sync id. COME cercarle nella propria API (query,
        pulizia del titolo, quanti risultati) è del provider; il provider non
        giudica: restituisce ciò che ha trovato, in ordine di rilevanza, anche
        niente (lista vuota). L'host sceglie quella che è davvero la traccia
        (titolo + tutti gli artisti + anno...) e ne ricava la disponibilità.
        Solleva se il servizio non risponde: l'host lo tratta come errore del
        provider, mai come "non trovata".
        """

    @abstractmethod
    def get_stream_url(self, sync_id: str, session: AuthSession) -> str | None:
        """Ritorna un URL streamabile per il dato sync_id."""

    def supports_likes(self) -> bool:
        """Whether ``like_track`` / ``unlike_track`` are implemented: the
        favorites of a user propagate to their own account on this provider.
        Declared, never inferred."""
        return False

    def like_track(self, sync_id: str, session: AuthSession) -> None:
        """Segna come 'piaciuta'/preferita la traccia sync_id sul sync provider.
        Override opzionale — no-op sui provider che non supportano questo concetto."""

    def unlike_track(self, sync_id: str, session: AuthSession) -> None:
        """Toglie il 'mi piace' messo con ``like_track``. Override opzionale —
        no-op sui provider che non supportano questo concetto. Non cancella
        mai il file."""

    def supports_playlists(self) -> bool:
        """Whether ``create_playlist`` / ``update_playlist`` / ``delete_playlist``
        are implemented: the playlists of a user (kept by Musizeer) are
        mirrored on their own account of this provider, where the server shows
        them natively. Declared, never inferred."""
        return False

    def create_playlist(self, name: str, sync_ids: list[str], session: AuthSession) -> str:
        """Optional (``supports_playlists``). Creates a playlist named ``name``
        holding the tracks ``sync_ids`` (this provider's ids, in order) on the
        account of ``session`` and returns the id the provider gives it. The
        host always uses the user's OWN account, never a shared one."""
        raise NotImplementedError

    def update_playlist(self, remote_id: str, name: str, sync_ids: list[str], session: AuthSession) -> None:
        """Optional (``supports_playlists``). Makes the playlist ``remote_id``
        be exactly ``name`` with the tracks ``sync_ids`` in that order (the
        host sends the whole content: a replacement, not a diff). Raise
        ``musizeer_sdk.errors.RemotePlaylistGone`` when it no longer exists
        there: the host creates it again."""
        raise NotImplementedError

    def delete_playlist(self, remote_id: str, session: AuthSession) -> None:
        """Optional (``supports_playlists``). Deletes the playlist ``remote_id``
        (never the tracks, which stay in the library). ``RemotePlaylistGone``
        when it was not there: the host takes that as done."""
        raise NotImplementedError

    def list_library(self, session: AuthSession) -> list[LibraryItem]:
        """
        Elenca l'INTERA libreria già indicizzata da questo sync provider (path,
        tag, info tecniche) senza scaricare alcun byte — usato dal dedup di
        libreria per ottenere candidati economicamente. Vale come fonte solo se
        questo sync provider punta alla STESSA libreria dello storage
        provider attivo.

        Override opzionale: un provider che non sa elencare la libreria (o
        solo in modalità "meta", non source of truth) resta al default —
        lista vuota, il chiamante generico salta il dedup per quel provider."""
        return []

    def fetch_partial(
        self, sync_id: str, workspace: str, session: AuthSession, max_bytes: int | None = None,
    ) -> str | None:
        """
        Scrive in ``workspace`` una copia (o i primi ``max_bytes`` byte, se il
        provider sa farlo) del file audio identificato da ``sync_id`` e ne
        ritorna il nome; l'host rimuove la cartella a fine uso — per un'elaborazione che
        ha bisogno dei byte reali, es. il fingerprinting del dedup di
        libreria (analizza solo i primi ~120s, mai il file intero).

        ATTENZIONE: va per ``sync_id``, MAI per il path riportato da
        ``list_library`` — quel path è spesso un path VIRTUALE che il sync
        provider ricostruisce dai tag (es. Navidrome via Subsonic), non la
        posizione reale del file: seguirlo per leggere byte falisce quasi
        sempre silenziosamente (visto in verifica live). Il sync provider è
        l'unico che sa risolvere il proprio id in byte reali, indipendentemente
        da come lo storage provider organizza i file.

        Un provider che non sa fare fetch parziale può ignorare ``max_bytes``
        e scaricare tutto: è solo un'ottimizzazione, mai un requisito di
        correttezza.

        Default: non supportato -> None (il chiamante salta la conferma via
        fingerprint per quel file, restando conservativo — mai un dedup
        "al buio")."""
        return None


# ------------------------------------------------------------------
# Identification provider (fingerprint audio → identità reale)
# ------------------------------------------------------------------


@dataclass
class Identity:
    """Identità reale di un file audio, dedotta dall'audio (non dai tag).

    ATTENZIONE: album/anno da fingerprint NON sono affidabili (un'impronta
    corrisponde a decine di recording) → il contratto espone solo titolo/artista
    più il MusicBrainz recording id e lo score. L'album resta ad altri provider.
    """

    title: str
    artist: str
    score: float
    recording_id: str | None = None


class IdentificationProvider(BaseProvider, ABC):
    """
    Identifica *cosa* è davvero un file audio a partire dall'impronta acustica.
    Usato per l'anti-mislabel post-download e per il dedup affidabile.
    """

    def is_available(self) -> bool:
        """True se il provider è utilizzabile (es. API key presente).
        Default: non disponibile → il chiamante lo salta (graceful no-op)."""
        return False

    @abstractmethod
    def identify(self, audio: WorkspaceFile) -> Identity | None:
        """Miglior corrispondenza trovata per il file ``audio`` (nella
        cartella di lavoro dell'host), col proprio ``score`` grezzo — o None se non identificabile/non disponibile/lookup fallito.
        Non applica MAI una soglia di confidenza: quella è una policy
        dell'host,
        non conoscenza del provider."""

