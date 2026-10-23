"""
Cosa può fare chi sta guardando, calcolato solo lato server: la modalità
(``open`` con l'autenticazione disattivata, poi ``guest``, ``user``,
``admin``) e le capability che ne derivano. È il contratto che la UI usa per
mostrare o nascondere le azioni; le rotte restano protette dalle proprie
dipendenze, e ``tests/test_capabilities.py`` verifica che le due cose
concordino.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from db.models import SYSTEM_USER_ID, User

Mode = Literal["open", "guest", "user", "admin"]


@dataclass(frozen=True)
class Capabilities:
    search: bool
    # Scaricare una traccia e aggiungerla ai preferiti quando arriva.
    download: bool
    # Scaricare senza aggiungere ai preferiti: solo admin (e modalità aperta).
    download_plain: bool
    # Il cuore: aggiungere/togliere dai preferiti una traccia in libreria.
    favorite: bool
    stream_full: bool
    library: bool
    # Le proprie playlist (GET /playlists): private di chi le ha, anche
    # per l'utente di sistema in modalità aperta (è l'istanza intera).
    playlists: bool
    settings_personal: bool
    # Collegare i propri account dei provider (GET /auth/linked-accounts): ogni
    # account reale, e l'utente di sistema in modalità aperta (il suo account
    # è quello dell'istanza intera).
    linked_accounts: bool
    admin: bool

    def as_dict(self) -> dict[str, bool]:
        return asdict(self)


_BY_MODE: dict[str, Capabilities] = {
    "guest": Capabilities(
        search=False, download=False, download_plain=False, favorite=False,
        stream_full=False, library=False, playlists=False, settings_personal=False, linked_accounts=False,
        admin=False,
    ),
    "user": Capabilities(
        search=True, download=True, download_plain=False, favorite=True,
        stream_full=True, library=True, playlists=True,
        settings_personal=True, linked_accounts=True, admin=False,
    ),
    "admin": Capabilities(
        search=True, download=True, download_plain=True, favorite=True,
        stream_full=True, library=True, playlists=True,
        settings_personal=True, linked_accounts=True, admin=True,
    ),
    # Con l'autenticazione disattivata c'e' UNA persona, il proprietario
    # dell'istanza (l'utente di sistema): ha tutto quello di un admin con
    # un account, comprese le impostazioni personali (salvate per
    # ``SYSTEM_USER_ID``). Cio' che ha senso solo con gli account (uscire,
    # cambiare la propria password, gestire gli altri utenti) non esiste, ma
    # non e' una capability: la UI lo toglie dal modo, non da qui.
    "open": Capabilities(
        search=True, download=True, download_plain=True, favorite=True,
        stream_full=True, library=True, playlists=True,
        settings_personal=True, linked_accounts=True, admin=True,
    ),
}


def viewer_mode(auth_enabled: bool, user: User | None) -> Mode:
    if not auth_enabled:
        return "open"
    if user is None:
        return "guest"
    return "admin" if user.role == "admin" else "user"


def capabilities_for(mode: Mode) -> Capabilities:
    return _BY_MODE[mode]


@dataclass(frozen=True)
class Viewer:
    """Chi guarda una lista di tracce, per lo stato per-utente di ogni traccia
    (``services.track_state``): di chi sono i preferiti (``None`` per un
    ospite; l'utente di sistema in modalità aperta) e se può ascoltare
    l'audio completo."""

    user_id: str | None
    stream_full: bool

    @classmethod
    def of(cls, auth_enabled: bool, user: User | None) -> "Viewer":
        mode = viewer_mode(auth_enabled, user)
        user_id = SYSTEM_USER_ID if mode == "open" else (user.id if user else None)
        return cls(user_id=user_id, stream_full=capabilities_for(mode).stream_full)


def favorite_on_arrival(user: User, requested: bool | None) -> bool:
    """Se un download chiesto da ``user`` aggiunge la traccia ai preferiti
    quando arriva. Per un utente non admin SEMPRE (nessun parametro lo
    spegne: il download semplice è solo degli admin, ``download_plain``);
    un admin (e la modalità aperta) sceglie esplicitamente, di default no."""
    if not capabilities_for("admin" if user.role == "admin" else "user").download_plain:
        return True
    return bool(requested)
