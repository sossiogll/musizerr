"""
Query sui record ``Download`` condivise da route e worker.

Una traccia ha al più UNA richiesta di download aperta: la libreria è unica,
quindi due richieste parallele per la stessa traccia producono due copie dello
stesso brano (spesso con provider e percorsi diversi). Chi accoda lavoro
chiede prima ``find_open_download`` e, se c'è, si aggancia a quella: chi si
aggancia alla richiesta di un altro utente diventa un ``DownloadFollower``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import and_, case, exists, func, or_
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, select

from core import download_states as ds
from db.expressions import descending
from db.library_store import library_clause
from db.models import Download, DownloadFollower
from db.upsert import insert_ignore


def track_status_ranks(user_id: str | None = None, sync_id_types: Iterable[str] = ()):
    """Sottoquery ``(track_id, rank)``: per ogni traccia con dei tentativi, la
    posizione in ``ds.ORDER`` del suo stato del file — ``downloaded`` se è in
    libreria (``db.library_store``, qualunque fonte), altrimenti il più
    avanti fra i tentativi: lo stesso calcolo di :func:`aggregate_status`, in
    SQL. Con ``user_id``, solo i tentativi che quell'utente vede
    (:func:`visible_to_user_clause`). Fuori dalla libreria un tentativo
    arrivato vale come fallito (il suo file è andato perso, vedi
    ``ds.file_state``)."""
    lost = ds.ORDER.index(ds.FAILED)
    rank = case(
        {status: lost if status == ds.DOWNLOADED else index for index, status in enumerate(ds.ORDER)},
        value=Download.status, else_=len(ds.ORDER),
    )
    file_rank = case(
        (Download.track_id.in_(library_clause(sync_id_types)), ds.ORDER.index(ds.DOWNLOADED)),
        else_=func.min(rank),
    )
    query = select(Download.track_id.label("track_id"), file_rank.label("rank")).group_by(Download.track_id)
    if user_id is not None:
        query = query.where(visible_to_user_clause(user_id))
    return query.subquery()


def ranks_of_phase(phase: str) -> list[int]:
    return [index for index, status in enumerate(ds.ORDER) if ds.LABELS[status]["phase"] == phase]


def status_summary(
    session: Session, user_id: str | None, *, since: datetime, sync_id_types: Iterable[str] = (),
) -> dict:
    """Quante tracce per stato del file e per fase, e quante sono arrivate
    in libreria da ``since``. Stessa visibilità di ``GET /downloads``."""
    ranks = track_status_ranks(user_id, sync_id_types)
    by_status = {status: 0 for status in ds.ORDER}
    for rank, count in session.exec(select(ranks.c.rank, func.count()).group_by(ranks.c.rank)).all():
        if rank < len(ds.ORDER):
            by_status[ds.ORDER[rank]] = count
    by_phase = {phase: 0 for phase in ds.PHASES}
    for status, count in by_status.items():
        by_phase[ds.LABELS[status]["phase"]] += count

    landed = select(func.count(func.distinct(Download.track_id))).where(
        Download.status == ds.DOWNLOADED, Download.completed_at >= since,
    )
    if user_id is not None:
        landed = landed.where(visible_to_user_clause(user_id))
    return {
        "statuses": by_status,
        "phases": by_phase,
        "total": sum(by_status.values()),
        "downloaded_since": session.exec(landed).one(),
    }


def open_clause():
    """Una richiesta di download ancora aperta (in coda, in corso, delegata con handle)."""
    # Un DISPATCHED senza handle non può più avanzare: nessuno sa chiedere al
    # sistema esterno come sia finita (righe precedenti alla riconciliazione).
    # Contarlo come aperto bloccherebbe la traccia per sempre.
    return or_(
        Download.status.in_(tuple(ds.OPEN - {ds.DISPATCHED})),
        and_(Download.status == ds.DISPATCHED, Download.delegation_handle.is_not(None)),
    )


def find_open_download(session: Session, track_id: str) -> Download | None:
    """La richiesta ancora in corso per questa traccia, se esiste."""
    return session.exec(
        select(Download).where(Download.track_id == track_id, open_clause())
    ).first()


def latest_landing(session: Session, track_id: str) -> datetime | None:
    """Quando la traccia è arrivata in libreria l'ultima volta (None se mai)."""
    return session.exec(
        select(Download.completed_at)
        .where(Download.track_id == track_id, Download.status == ds.DOWNLOADED)
        .order_by(descending(Download.completed_at))
    ).first()


def aggregate_status(attempts: list[Download], in_library: bool) -> str | None:
    """Lo stato del file di una traccia dai suoi tentativi (vince il più
    avanti nell'ordine canonico), ``downloaded`` se è in libreria."""
    return ds.file_state((a.status for a in attempts), in_library)


def mark_unqueued(session: Session, download_id: str, reason: str, at: datetime) -> None:
    """The download could not be handed to the task queue (the broker is
    down): no worker will ever run it, so it must not stay an open request that
    later requests join. It ends as a transient failure — ``failed``,
    ``retriable`` — with the ``ds.QUEUE_UNAVAILABLE`` marker in its error text:
    the file state shows it as failed, the requester and the followers keep
    their history, and the transient-failure routine queues it again."""
    download = session.get(Download, download_id)
    if download is None:
        return
    download.status = ds.FAILED
    download.retriable = True
    download.error_message = f"{ds.QUEUE_UNAVAILABLE}: {reason}"
    download.completed_at = at
    session.add(download)
    session.commit()


def add_follower(session: Session, download_id: str, user_id: str) -> None:
    """Registra chi si aggancia a una richiesta altrui (idempotente, anche con
    due richieste insieme: decide il vincolo unico)."""
    insert_ignore(session, DownloadFollower(download_id=download_id, user_id=user_id))
    session.commit()


def visible_to_user_clause(user_id: str) -> ColumnElement:
    """Righe ``Download`` che l'utente ha richiesto o a cui si è agganciato.
    Filtra per singola riga, mai per traccia: un follower non deve vedere i
    tentativi di altri utenti sulla stessa traccia."""
    follower_exists = exists().where(
        DownloadFollower.download_id == Download.id,
        DownloadFollower.user_id == user_id,
    )
    return or_(Download.requested_by == user_id, follower_exists)
