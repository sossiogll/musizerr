"""I preferiti di un utente (``user_favorites``). Solo accesso ai dati: la
regola "solo tracce in libreria" la applica ``services.favorites``."""
from __future__ import annotations

import uuid
from typing import Iterable

from sqlmodel import Session, select

from db.models import RemovedFavorite, UserFavorite
from db.upsert import insert_ignore


def add_favorite(session: Session, user_id: str, track_id: str, source: str) -> bool:
    """Aggiunge il preferito; False se c'era già (idempotente, anche con due
    richieste insieme: decide il vincolo unico)."""
    added = insert_ignore(session, UserFavorite(user_id=user_id, track_id=track_id, source=source))
    session.commit()
    return added == 1


def remove_favorite(session: Session, user_id: str, track_id: str) -> bool:
    """Toglie il preferito; False se non c'era (idempotente)."""
    row = find_favorite(session, user_id, track_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def find_favorite(session: Session, user_id: str, track_id: str) -> UserFavorite | None:
    return session.exec(
        select(UserFavorite).where(UserFavorite.user_id == user_id, UserFavorite.track_id == track_id)
    ).first()


def favorite_track_ids(session: Session, user_id: str, track_ids: Iterable[str]) -> set[str]:
    """Fra ``track_ids``, quelle che l'utente ha nei preferiti."""
    ids = list(track_ids)
    if not ids:
        return set()
    return set(session.exec(
        select(UserFavorite.track_id).where(UserFavorite.user_id == user_id, UserFavorite.track_id.in_(ids))
    ).all())


# ------------------------------------------------------------------
# Removed favorites (``RemovedFavorite``): what the user took off stays off
# ------------------------------------------------------------------

_KEY_CHUNK = 500


def record_removal(session: Session, user_id: str, keys: Iterable[str]) -> None:
    """The user took the song named by ``keys`` off their favorites. Keys
    already recorded are kept as they are (their group, their date)."""
    wanted = {key for key in keys if key}
    if not wanted:
        return
    group = str(uuid.uuid4())
    insert_ignore(session, *(RemovedFavorite(user_id=user_id, key=key, group_id=group) for key in sorted(wanted)))
    session.commit()


def is_removed(session: Session, user_id: str, keys: Iterable[str]) -> bool:
    """Whether the user took off a song that shares a key with ``keys``."""
    wanted = list({key for key in keys if key})
    for start in range(0, len(wanted), _KEY_CHUNK):
        found = session.exec(
            select(RemovedFavorite.id).where(
                RemovedFavorite.user_id == user_id, RemovedFavorite.key.in_(wanted[start:start + _KEY_CHUNK])
            )
        ).first()
        if found is not None:
            return True
    return False


def clear_removal(session: Session, user_id: str, keys: Iterable[str]) -> int:
    """The user favorites the song again (a heart, a download): every removal
    that shares a key with ``keys`` is forgotten, with all its keys. Returns
    how many keys went."""
    wanted = list({key for key in keys if key})
    groups: set[str] = set()
    for start in range(0, len(wanted), _KEY_CHUNK):
        groups.update(session.exec(
            select(RemovedFavorite.group_id).where(
                RemovedFavorite.user_id == user_id, RemovedFavorite.key.in_(wanted[start:start + _KEY_CHUNK])
            )
        ).all())
    if not groups:
        return 0
    rows = session.exec(
        select(RemovedFavorite).where(RemovedFavorite.user_id == user_id, RemovedFavorite.group_id.in_(groups))
    ).all()
    for row in rows:
        session.delete(row)
    session.commit()
    return len(rows)
