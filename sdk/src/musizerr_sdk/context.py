"""
Canale dichiarativo fra un provider e l'host. Insieme ad ``AuthSession`` (per
singola chiamata) e ai valori di ritorno dei metodi del contratto, è
l'UNICO modo con cui un provider parla con Musizerr — nessun import di
moduli dell'host, nessuna lettura di variabile d'ambiente, nessuna
conoscenza di come/dove l'host persiste le proprie impostazioni. L'host
inietta un'implementazione concreta alla costruzione del provider
(``BaseProvider.__init__``); i test e chi sviluppa un provider fuori
dall'host usano ``StaticProviderContext``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

from . import urls

# The default of each timeout. The host's settings catalog takes its defaults
# from these, so a provider tested against ``StaticProviderContext`` runs with
# the very values an untouched installation gives it.
DEFAULT_HTTP_TIMEOUT = 10
DEFAULT_DOWNLOAD_TIMEOUT = 60
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_STREAM_START_TIMEOUT = 5
DEFAULT_API_PREFIX = "/api"


@dataclass(frozen=True)
class ContextSnapshot:
    """Everything a provider's ``ProviderContext`` answers, as plain data: the
    host takes one before each call and sends it with the call, and the runner
    builds the provider's context from it for the duration of that call. The
    provider never calls back into the host, and a change of a setting is seen
    by the next call, never by one already running."""

    settings: dict[str, Any] = field(default_factory=dict)
    http_timeout: int = DEFAULT_HTTP_TIMEOUT
    download_timeout: int = DEFAULT_DOWNLOAD_TIMEOUT
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT
    stream_start_timeout: int = DEFAULT_STREAM_START_TIMEOUT
    # The prefix under which the frontend reaches the host's API: what
    # ``cover_url`` / ``stream_url`` / ``preview_url`` are built with.
    api_prefix: str = DEFAULT_API_PREFIX


@runtime_checkable
class ProviderContext(Protocol):
    def settings(self) -> Mapping[str, Any]:
        """Istantanea di TUTTE le proprie impostazioni (short key -> valore),
        con UNA sola lettura. Un provider che ha bisogno di più chiavi per
        una singola operazione chiama questo una volta, mai ``setting()`` per
        ciascuna chiave."""
        ...

    def setting(self, key: str, default: Any = None) -> Any:
        """Valore della propria impostazione ``key`` (la short key di
        ``provider.<nome>.<key>``), o ``default`` se non impostata. Per più
        chiavi in una stessa operazione usare ``settings()``."""
        ...

    @property
    def http_timeout(self) -> int:
        """Timeout (s) per una chiamata HTTP verso l'API del provider."""
        ...

    @property
    def download_timeout(self) -> int:
        """Timeout (s) per lo streaming del singolo file scaricato."""
        ...

    @property
    def connect_timeout(self) -> int:
        """Timeout (s) per aprire una connessione al proprio servizio (es. il
        canale di controllo di un server di file): breve, così un servizio
        irraggiungibile risponde subito "non raggiungibile" invece di tenere
        occupato un worker per il timeout di un trasferimento."""
        ...

    @property
    def stream_start_timeout(self) -> int:
        """Timeout (s) di ogni passo prima del primo byte di uno stream che
        qualcuno sta ascoltando (aprire la connessione, accedere, aprire il
        trasferimento): più breve degli altri, chi ascolta aspetta. Il
        trasferimento, una volta partito, usa ``download_timeout``."""
        ...

    def cover_url(self, cover_id: str) -> str:
        """URL pubblico con cui il browser ottiene una propria cover tramite
        l'host (che la chiede a ``get_cover_url``)."""
        ...

    def stream_url(self, item_id: str) -> str:
        """URL pubblico con cui il browser ascolta un proprio brano tramite
        l'host (che lo chiede a ``get_stream_url``)."""
        ...

    def preview_url(self, item_id: str) -> str:
        """URL pubblico con cui il browser ascolta l'anteprima di un proprio
        brano tramite l'host, che a ogni richiesta chiede l'URL aggiornato a
        ``get_preview_url``: per le anteprime il cui URL firmato scade."""
        ...



class StaticProviderContext:
    """Contesto fisso, per i test e per chi sviluppa/prova un provider senza
    l'host: nessuna lettura di config reale, solo i valori passati (o i
    default dei timeout di questo modulo, mai un numero scritto a mano qui).
    Classe semplice, non un dataclass: il metodo ``settings()`` del Protocol
    e un campo dati chiamato allo stesso modo non possono coesistere su una
    stessa classe (l'istanza generata da un dataclass field sovrascriverebbe
    il metodo), quindi la mappa passata al costruttore vive in un attributo
    privato."""

    def __init__(
        self,
        settings: Mapping[str, Any] | None = None,
        http_timeout: int | None = None,
        download_timeout: int | None = None,
        connect_timeout: int | None = None,
        stream_start_timeout: int | None = None,
        provider_name: str = "provider",
        api_prefix: str = DEFAULT_API_PREFIX,
    ) -> None:
        self._settings = dict(settings) if settings else {}
        self._provider_name = provider_name
        self._api_prefix = api_prefix
        self._http_timeout = http_timeout if http_timeout is not None else DEFAULT_HTTP_TIMEOUT
        self._download_timeout = download_timeout if download_timeout is not None else DEFAULT_DOWNLOAD_TIMEOUT
        self._connect_timeout = connect_timeout if connect_timeout is not None else DEFAULT_CONNECT_TIMEOUT
        self._stream_start_timeout = (
            stream_start_timeout if stream_start_timeout is not None else DEFAULT_STREAM_START_TIMEOUT
        )

    def snapshot(self) -> ContextSnapshot:
        """The values of this context as the data a call carries (what a host
        sends along with a call to the provider's runner)."""
        return ContextSnapshot(
            settings=dict(self._settings),
            http_timeout=self._http_timeout,
            download_timeout=self._download_timeout,
            connect_timeout=self._connect_timeout,
            stream_start_timeout=self._stream_start_timeout,
            api_prefix=self._api_prefix,
        )

    def settings(self) -> Mapping[str, Any]:
        return MappingProxyType(self._settings)

    def setting(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, default)

    def cover_url(self, cover_id: str) -> str:
        return urls.cover_url(self._api_prefix, self._provider_name, cover_id)

    def stream_url(self, item_id: str) -> str:
        return urls.provider_stream_url(self._api_prefix, self._provider_name, item_id)

    def preview_url(self, item_id: str) -> str:
        return urls.preview_url(self._api_prefix, self._provider_name, item_id)

    @property
    def http_timeout(self) -> int:
        return self._http_timeout

    @property
    def download_timeout(self) -> int:
        return self._download_timeout

    @property
    def connect_timeout(self) -> int:
        return self._connect_timeout

    @property
    def stream_start_timeout(self) -> int:
        return self._stream_start_timeout
