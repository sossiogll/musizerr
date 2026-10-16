"""
Accesso al marcatore durevole ``PendingLike``: un solo intento per (utente,
traccia), in due fasi (vedi ``db.models.PendingLike``). Idempotente: creare
due volte lo stesso intento non duplica nulla.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import or_, update
from sqlmodel import Session, select

from db.models import PendingLike
from db.upsert import insert_ignore


def find_pending_like(session: Session, user_id: str, track_id: str) -> PendingLike | None:
    return session.exec(
        select(PendingLike).where(PendingLike.user_id == user_id, PendingLike.track_id == track_id)
    ).first()


def ensure_pending_like(
    session: Session,
    user_id: str,
    track_id: str,
    sync_provider: str | None = None,
) -> str:
    """
    Registra l'intento dell'utente su questa traccia, se non c'è già, e ne
    ritorna l'id. Senza ``sync_provider`` è "aggiungila ai preferiti quando
    arriva in libreria"; con ``sync_provider`` è "propaga il like a quel sync
    provider". Un intento esistente passa alla fase sync quando serve, mai
    all'indietro.
    """
    marker = PendingLike(user_id=user_id, track_id=track_id, sync_provider=sync_provider)
    # The unique (user, track) decides when two requests register it together.
    inserted = insert_ignore(session, marker)
    session.commit()
    if inserted:
        return marker.id

    existing = find_pending_like(session, user_id, track_id)
    if sync_provider and existing.sync_provider != sync_provider:
        # Una fase nuova: si riparte da capo, con un tentativo subito.
        existing.sync_provider = sync_provider
        existing.attempts = 0
        existing.next_attempt_at = None
        session.add(existing)
        session.commit()
    return existing.id


def delete_pending_like(session: Session, user_id: str, track_id: str) -> None:
    marker = find_pending_like(session, user_id, track_id)
    if marker is not None:
        session.delete(marker)
        session.commit()


def favorite_intents(session: Session, track_id: str) -> list[PendingLike]:
    """Gli intenti "preferito all'arrivo" ancora aperti su una traccia, di
    qualunque utente."""
    return list(session.exec(
        select(PendingLike).where(PendingLike.track_id == track_id, PendingLike.sync_provider.is_(None))
    ).all())


def tracks_with_favorite_intent(session: Session, user_id: str, track_ids: Iterable[str]) -> set[str]:
    """Fra ``track_ids``, quelle su cui l'utente ha un intento "preferito all'arrivo"."""
    ids = list(track_ids)
    if not ids:
        return set()
    return set(session.exec(
        select(PendingLike.track_id).where(
            PendingLike.user_id == user_id,
            PendingLike.track_id.in_(ids),
            PendingLike.sync_provider.is_(None),
        )
    ).all())


def list_pending_like_ids(session: Session) -> list[str]:
    """Tutti i marcatori pendenti."""
    return list(session.exec(select(PendingLike.id)).all())


def claim_pending_like(session: Session, marker_id: str, due_by: datetime, hold_until: datetime) -> bool:
    """
    Prende il marcatore per UN tentativo, atomicamente: riesce solo se
    nessun altro tentativo lo tiene né è schedulato più avanti
    (``next_attempt_at`` NULL o non oltre ``due_by``), e lo tiene fino a
    ``hold_until``. False se il marcatore non c'è o non è dovuto: chi
    chiama si ferma senza toccare nulla (single-flight).
    """
    result = session.exec(
        update(PendingLike)
        .where(
            PendingLike.id == marker_id,
            or_(PendingLike.next_attempt_at.is_(None), PendingLike.next_attempt_at <= due_by),
        )
        .values(next_attempt_at=hold_until)
    )
    session.commit()
    return result.rowcount > 0


def mark_queued(session: Session, marker_id: str, at: datetime, *, lost_before: datetime | None = None) -> bool:
    """Prima di accodare un tentativo: ``next_attempt_at`` diventa ``at`` (il
    tentativo è in coda, dovuto adesso), atomicamente, solo se il marcatore
    non ha già una chain viva — mai schedulato (NULL), o con un tentativo
    dovuto entro ``at`` (``lost_before`` più stretto: solo uno perso da
    prima di allora). False: c'è già un tentativo in coda o in programma,
    non se ne accoda un altro. Lo sweep e i trigger successivi lo saltano
    finché il tentativo accodato non parte (o non è dato per perso)."""
    bound = lost_before if lost_before is not None else at
    result = session.exec(
        update(PendingLike)
        .where(
            PendingLike.id == marker_id,
            or_(PendingLike.next_attempt_at.is_(None), PendingLike.next_attempt_at < bound),
        )
        .values(next_attempt_at=at)
    )
    session.commit()
    return result.rowcount > 0


def list_resumable_pending_like_ids(session: Session, lost_before: datetime) -> list[str]:
    """I marcatori senza una chain viva: mai schedulati (NULL) o con un
    tentativo che doveva partire prima di ``lost_before`` e non è mai
    arrivato (worker riavviato, task perso)."""
    return list(session.exec(
        select(PendingLike.id).where(
            or_(PendingLike.next_attempt_at.is_(None), PendingLike.next_attempt_at < lost_before)
        )
    ).all())


def list_pending_like_ids_for_track(session: Session, track_id: str) -> list[str]:
    """Gli intenti di propagazione al sync ancora aperti su una traccia."""
    return list(session.exec(
        select(PendingLike.id).where(PendingLike.track_id == track_id, PendingLike.sync_provider.is_not(None))
    ).all())
