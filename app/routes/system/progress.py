from __future__ import annotations

import json
import time
from typing import AsyncIterator, Callable

import anyio
import redis.asyncio as aioredis
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from db.download_store import visible_to_user_clause
from db.engine import engine
from db.models import Download, User
from services.progress import PROGRESS_CHANNEL, REDIS_URL

from ..deps import StreamUserDep

router = APIRouter()

_KEEPALIVE_SECONDS = 15.0
# Quanto a lungo un download non visibile resta tale senza rileggere il DB:
# chi si aggancia a una richiesta altrui ne vede gli eventi entro questo tempo.
_HIDDEN_TTL_SECONDS = 5.0
KEEPALIVE_FRAME = ": keepalive\n\n"


@router.get("/progress")
async def progress_stream(user: StreamUserDep):
    """
    SSE degli aggiornamenti di download pubblicati dal worker su Redis.

    Autenticato come l'audio completo (``get_stream_user``): l'EventSource
    del browser non può mandare header, quindi usa il cookie di stream; un
    client API può usare il bearer. Ogni utente riceve solo gli eventi dei
    download che vede anche in ``GET /downloads``; un admin, o chiunque con
    l'autenticazione disattivata, li riceve tutti.

    Formato evento:
        data: {"download_id": "...", "status": "<core.download_states>", ...}
        data: {"type": "library_job", "user_id": "...", "run": {...}}  (solo al proprietario)
    """
    return StreamingResponse(
        sse_frames(_redis_messages(), user, _download_visibility(user)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def event_visible_to(event: dict, user: User, is_download_visible: Callable[[str], bool]) -> bool:
    """Se ``user`` può ricevere ``event``. Un evento con ``user_id`` è
    destinato solo a quell'utente, admin compresi (es. il progresso dei suoi
    job di libreria: un admin non deve vederlo come proprio); uno con
    ``download_id`` segue la visibilità di quel download; qualunque altro
    evento è solo per gli admin."""
    owner = event.get("user_id")
    if owner is not None:
        return owner == user.id
    if user.role == "admin":
        return True
    download_id = event.get("download_id")
    return bool(download_id) and is_download_visible(download_id)


async def sse_frames(
    messages: AsyncIterator[str | None],
    user: User,
    is_download_visible: Callable[[str], bool],
) -> AsyncIterator[str]:
    """Frame SSE per ``user`` dai messaggi grezzi del canale; None vale
    "nessun messaggio entro l'intervallo" e produce un keepalive. La
    visibilità può leggere il DB, quindi si valuta fuori dall'event loop."""
    async for raw in messages:
        if raw is None:
            yield KEEPALIVE_FRAME
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict) and await anyio.to_thread.run_sync(event_visible_to, event, user, is_download_visible):
            yield f"data: {raw}\n\n"


def _download_visibility(user: User) -> Callable[[str], bool]:
    """Visibilità dei download per ``user``, con la stessa regola di
    ``GET /downloads``. Un download visibile resta tale; uno non visibile si
    ricontrolla dopo ``_HIDDEN_TTL_SECONDS``, così chi si aggancia a una
    richiesta dopo l'apertura dello stream ne riceve gli eventi successivi
    senza una lettura del DB per ogni evento altrui."""
    visible: set[str] = set()
    hidden_until: dict[str, float] = {}

    def is_visible(download_id: str) -> bool:
        if download_id in visible:
            return True
        now = time.monotonic()
        if hidden_until.get(download_id, 0.0) > now:
            return False
        with Session(engine) as session:
            found = session.exec(
                select(Download.id).where(Download.id == download_id, visible_to_user_clause(user.id))
            ).first()
        if found is None:
            hidden_until[download_id] = now + _HIDDEN_TTL_SECONDS
            return False
        hidden_until.pop(download_id, None)
        visible.add(download_id)
        return True

    return is_visible


async def _redis_messages() -> AsyncIterator[str | None]:
    client = aioredis.from_url(REDIS_URL)
    pubsub = client.pubsub()
    await pubsub.subscribe(PROGRESS_CHANNEL)
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=_KEEPALIVE_SECONDS)
            if message is None:
                yield None
            elif message["type"] == "message":
                yield message["data"].decode()
    finally:
        # Alla disconnessione il generatore è cancellato: senza lo scudo anche
        # questi await verrebbero cancellati e la connessione resterebbe aperta.
        with anyio.CancelScope(shield=True):
            await pubsub.aclose()
            await client.aclose()
