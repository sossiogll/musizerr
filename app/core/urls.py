"""
URL pubblici delle risorse servite dal backend (cover, stream). Il frontend
raggiunge l'API solo sotto un prefisso (``/api`` dietro Vite o nginx): un
path relativo alla root caricherebbe la pagina del frontend invece della
risorsa. Ogni URL di questo tipo si costruisce con il prefisso dato: quelli che un
provider mette sulle proprie tracce (cover, stream, anteprima) con i costruttori
di ``musizerr_sdk.urls``, gli altri qui.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote

from musizerr_sdk.urls import cover_url, join_path, preview_url, provider_stream_url


def stored_stream_url(prefix: str, track_id: str) -> str:
    """Audio completo di un file in libreria (``GET /stream/{track_id}``)."""
    return join_path(prefix, "stream", track_id)


def duplicate_member_stream_url(prefix: str, member_id: str) -> str:
    """Il file di UN membro di un gruppo di duplicati, per sceglierlo
    ascoltandolo (``GET /library/duplicates/members/{id}/stream``, admin)."""
    return join_path(prefix, "library", "duplicates", "members", member_id, "stream")


# ---------------------------------------------------------------------------
# Forma persistita. Un URL servito dall'host dipende dal prefisso pubblico
# del deploy (che può cambiare), quindi non si salva mai così com'è: nel DB
# va un riferimento senza prefisso (``hostref:/cover/provider/p/id``), che a
# ogni lettura si ricostruisce con i costruttori qui sopra e il prefisso
# corrente. Un URL assoluto (CDN del provider) resta com'è.
# ---------------------------------------------------------------------------

HOST_REF_SCHEME = "hostref:"

# Campi di una traccia serializzata che possono contenere un URL servito
# dall'host.
TRACK_URL_FIELDS = ("cover_url", "preview_url", "stream_url")

_COVER = "cover"
_PREVIEW = "preview"
_PROVIDER_STREAM = "provider_stream"
_STORED_STREAM = "stored_stream"


@dataclass(frozen=True)
class HostResource:
    """Una risorsa servita dall'host: ``route`` è ``cover``, ``preview``,
    ``provider_stream`` o ``stored_stream`` (senza provider)."""

    route: str
    item_id: str
    provider: str | None = None

    def url(self, prefix: str) -> str:
        if self.route == _COVER:
            return cover_url(prefix, self.provider, self.item_id)
        if self.route == _PREVIEW:
            return preview_url(prefix, self.provider, self.item_id)
        if self.route == _PROVIDER_STREAM:
            return provider_stream_url(prefix, self.provider, self.item_id)
        return stored_stream_url(prefix, self.item_id)


def is_host_served(value: str | None) -> bool:
    """URL relativo alla root (con o senza prefisso) o riferimento persistito."""
    if not value:
        return False
    return value.startswith(HOST_REF_SCHEME) or (value.startswith("/") and not value.startswith("//"))


def parse_host_url(value: str | None) -> HostResource | None:
    """La risorsa indicata da un URL dell'host, con qualunque prefisso
    (``/cover/...``, ``/api/cover/...``, ``/music/api/stream/...``) o da un
    riferimento ``hostref:``. ``None`` se non è uno dei percorsi noti."""
    if not is_host_served(value):
        return None
    path = value[len(HOST_REF_SCHEME):] if value.startswith(HOST_REF_SCHEME) else value
    path = path.split("?", 1)[0].split("#", 1)[0]
    segments = [unquote(s) for s in path.split("/")[1:]]
    if any(s == "" for s in segments):
        return None
    if len(segments) >= 4 and segments[-4] == "cover" and segments[-3] == "provider":
        return HostResource(_COVER, segments[-1], segments[-2])
    if len(segments) >= 3 and segments[-3] == "preview":
        return HostResource(_PREVIEW, segments[-1], segments[-2])
    if len(segments) >= 4 and segments[-4] == "stream" and segments[-3] == "provider":
        return HostResource(_PROVIDER_STREAM, segments[-1], segments[-2])
    if len(segments) >= 2 and segments[-2] == "stream":
        return HostResource(_STORED_STREAM, segments[-1])
    return None


def to_host_ref(value: str | None) -> str | None:
    """Forma da persistere: un URL dell'host diventa ``hostref:`` + percorso
    senza prefisso; un URL assoluto resta com'è; un percorso relativo che non
    è una risorsa nota non si salva (``None``)."""
    if not is_host_served(value):
        return value
    resource = parse_host_url(value)
    return HOST_REF_SCHEME + resource.url("") if resource else None


def publish(prefix: str, value: str | None) -> str | None:
    """Forma da restituire al client: un riferimento (o un URL dell'host
    letto da dati vecchi) si ricostruisce col prefisso dato; un URL assoluto
    resta com'è; un percorso relativo non riconosciuto non si restituisce."""
    if not is_host_served(value):
        return value
    resource = parse_host_url(value)
    return resource.url(prefix) if resource else None


def track_dict_for_storage(data: dict) -> dict:
    """``data`` (traccia serializzata) con i campi URL in forma persistita."""
    for field in TRACK_URL_FIELDS:
        if field in data:
            data[field] = to_host_ref(data[field])
    return data


def publish_track_dict(prefix: str, data: dict) -> dict:
    """``data`` (traccia letta dal DB) con i campi URL in forma pubblica."""
    for field in TRACK_URL_FIELDS:
        if field in data:
            data[field] = publish(prefix, data[field])
    return data
