from __future__ import annotations

import logging
import re
import threading
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from sqlmodel import select

from musizerr_sdk.auth import AuthSession
from core.auth_manager import AuthError
from musizerr_sdk.interfaces import StorageProvider, StorageStreamResult
from core.identifiers import sync_id_type
from db.library_store import stored_file_paths
from db.models import Download, DuplicateMember, TrackIdentifierRecord, TrackRecord
from db.track_store import track_data_dict
from routes.deps import (
    AuthManagerDep,
    ConfigDep,
    OptionalUserDep,
    RegistryDep,
    SessionDep,
    StreamAdminDep,
    StreamCooldownDep,
    StreamUserDep,
)
from services import public_urls
from services.provider_calls import ProviderCallError, provider_call
from services.provider_proxy import ProviderCallFailed
from services.storage import resolve_storage_provider
from services.track_state import sync_id_types
from services.workspace import Workspace, WorkspaceError, new_workspace

router = APIRouter()
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_PASSTHROUGH_HEADERS = ("content-length", "content-range", "accept-ranges")

# What the proxy may pass on: a cover is an image, a stream is audio (Ogg
# included). An origin that answers 200 with anything else — typically an
# error document in JSON, XML or HTML — did not deliver what was asked, and
# that is decided before any header reaches the client.
IMAGE_TYPES = ("image/",)
AUDIO_TYPES = ("audio/", "application/ogg")
OCTET_STREAM = "application/octet-stream"

# Covers the origin said it does not have, not asked again for a while: a
# page full of albums without artwork would otherwise ask for each one on
# every render.
MISSING_COVER_TTL_S = 300
_MISSING_COVERS_MAX = 4096
_missing_covers: dict[tuple[str, str], float] = {}
_missing_covers_lock = threading.Lock()


def _cover_known_missing(provider: str, cover_id: str) -> bool:
    with _missing_covers_lock:
        until = _missing_covers.get((provider, cover_id))
        if until is None:
            return False
        if until < time.monotonic():
            del _missing_covers[(provider, cover_id)]
            return False
        return True


def _remember_missing_cover(provider: str, cover_id: str) -> None:
    with _missing_covers_lock:
        if len(_missing_covers) >= _MISSING_COVERS_MAX:
            _missing_covers.clear()
        _missing_covers[(provider, cover_id)] = time.monotonic() + MISSING_COVER_TTL_S


def clear_missing_covers() -> None:
    with _missing_covers_lock:
        _missing_covers.clear()


# Preview URLs just resolved, reused by the requests of one listen (the audio
# element asks again to seek): far shorter than any signature they carry. A
# reused URL that fails is resolved again before answering.
PREVIEW_URL_TTL_S = 120
_PREVIEW_URLS_MAX = 1024
_preview_urls: dict[tuple[str, str], tuple[float, str]] = {}
_preview_urls_lock = threading.Lock()


def _recent_preview_url(provider: str, item_id: str) -> str | None:
    with _preview_urls_lock:
        entry = _preview_urls.get((provider, item_id))
        if entry is None:
            return None
        if entry[0] < time.monotonic():
            del _preview_urls[(provider, item_id)]
            return None
        return entry[1]


def _remember_preview_url(provider: str, item_id: str, url: str | None) -> None:
    with _preview_urls_lock:
        if url is None:
            _preview_urls.pop((provider, item_id), None)
            return
        if len(_preview_urls) >= _PREVIEW_URLS_MAX:
            _preview_urls.clear()
        _preview_urls[(provider, item_id)] = (time.monotonic() + PREVIEW_URL_TTL_S, url)


def clear_preview_urls() -> None:
    with _preview_urls_lock:
        _preview_urls.clear()


class UnexpectedContent(Exception):
    """The origin answered 200 with something that is not what was asked."""


def _upstream_client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=120)


def _media_type(resp: httpx.Response) -> str:
    return resp.headers.get("content-type", "").split(";")[0].strip().lower() or OCTET_STREAM


def _accepted(media_type: str, accept: tuple[str, ...]) -> bool:
    return any(media_type.startswith(a) if a.endswith("/") else media_type == a for a in accept)


def _peek(resp: httpx.Response, limit: int = 200) -> bytes:
    """The start of a body that will not be served, for the log."""
    try:
        for chunk in resp.iter_bytes():
            return chunk[:limit]
    except httpx.HTTPError:
        pass
    return b""


def _proxy_stream(
    url: str,
    *,
    provider: str,
    accept: tuple[str, ...],
    subject: str = "provider",
    unexpected_kind: str = "upstream_error",
    headers: dict[str, str] | None = None,
    background: BackgroundTask | None = None,
    range_header: str | None = None,
) -> StreamingResponse:
    """Proxy di uno stream da un URL esterno, senza buffer. Il client HTTP è
    sincrono: i blocchi si leggono con un iteratore sincrono, che Starlette
    consuma in un thread, mai nell'event loop. La richiesta ``Range`` passa
    all'origine e la sua risposta parziale torna al client (seek).

    Tutto ciò che può andare storto si decide PRIMA di mandare gli header,
    con la convenzione dei fallimenti dei provider (``routes/errors.py``):
    un errore dell'origine (404, 5xx, irraggiungibile, lenta) o un tipo di
    contenuto fuori da ``accept`` (che vale ``unexpected_kind``) diventano
    :class:`ProviderCallError` a nome di ``provider``.

    Il corpo passa decodificato: si chiede ``Accept-Encoding: identity`` e,
    se l'origine comprime lo stesso, il ``Content-Length`` (quello compresso)
    non si inoltra — la risposta va in chunked. Se l'origine chiude a metà
    corpo si interrompe la connessione: il client vede un trasferimento
    incompleto, mai un file troncato ben formato."""
    request_headers = {"Accept-Encoding": "identity", **(headers or {})}
    if range_header:
        request_headers["Range"] = range_header
    client = _upstream_client()
    resp = None
    try:
        resp = client.send(client.build_request("GET", url, headers=request_headers), stream=True)
        resp.raise_for_status()
        media_type = _media_type(resp)
        if not _accepted(media_type, accept):
            raise UnexpectedContent(
                f"answered {resp.status_code} {media_type}: {_peek(resp)!r}"
            )
    except (httpx.HTTPError, UnexpectedContent) as exc:
        if resp is not None:
            resp.close()
        client.close()
        logger.warning("Upstream %s of %s failed: %s", url.split("?", 1)[0], provider, exc)
        kind = unexpected_kind if isinstance(exc, UnexpectedContent) else None
        raise ProviderCallError(provider, exc, subject, kind=kind) from exc

    encoded = resp.headers.get("content-encoding", "identity").lower() not in ("", "identity")
    response_headers = {"Accept-Ranges": "bytes"}
    for name in _PASSTHROUGH_HEADERS:
        if name in resp.headers and not (encoded and name == "content-length"):
            response_headers[name.title()] = resp.headers[name]

    def stream_and_close():
        try:
            yield from resp.iter_bytes(chunk_size=64 * 1024)
        except httpx.HTTPError as exc:
            # Gli header sono partiti: resta solo interrompere la connessione.
            logger.warning("Upstream %s of %s broke off mid-body: %s", url.split("?", 1)[0], provider, exc)
            raise
        finally:
            resp.close()
            client.close()

    return StreamingResponse(
        stream_and_close(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type") or OCTET_STREAM,
        headers=response_headers,
        background=background,
    )


def _storage(registry, config, auth_manager) -> tuple[StorageProvider | None, AuthSession | None]:
    """Lo storage provider attivo (services/storage.py, mai un nome scritto
    qui) e la sua sessione; anonima se l'autenticazione fallisce.
    ``(None, None)`` se non c'è nessuno storage."""
    storage, _name = resolve_storage_provider(registry, config.storage_provider)
    if storage is None:
        return None, None
    try:
        return storage, auth_manager.authenticate(storage)
    except AuthError:
        return storage, AuthSession.anonymous()


_BYTE_RANGE = re.compile(r"^bytes=(\d+)-(\d*)$")


def _range_start(range_header: str | None) -> int | None:
    """The first byte of a single ``bytes=N-`` (or ``N-M``) range; None for
    no range or any other form (suffix, several ranges): the whole file."""
    match = _BYTE_RANGE.match(range_header.strip()) if range_header else None
    return int(match.group(1)) if match else None


def _chunks_response(result: StorageStreamResult, cleanup: BackgroundTask, *, ranged: bool) -> Response:
    """Blocchi di byte dallo storage. Con la dimensione nota l'host ne manda
    la lunghezza; se lo storage sa ripartire da un offset
    (``supports_stream_offset``, ``ranged``) una richiesta Range è un 206
    da ``result.offset`` alla fine del file (o un 416 oltre la fine)."""
    headers: dict[str, str] = {"Accept-Ranges": "bytes"} if ranged else {}
    status = 200
    size, start = result.size, result.offset
    if size is not None:
        if ranged and start >= size > 0:
            close = getattr(result.chunks, "close", None)
            if close:
                close()
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"}, background=cleanup)
        headers["Content-Length"] = str(size - start)
        if ranged:
            status = 206
            headers["Content-Range"] = f"bytes {start}-{size - 1}/{size}"
    return StreamingResponse(
        result.chunks, status_code=status, media_type=result.media_type, headers=headers, background=cleanup,
    )


def _stored_file_response(
    result: StorageStreamResult, workspace: Workspace, range_header: str | None = None, *, provider: str,
    seekable: bool = False,
) -> Response:
    """Risposta HTTP per il risultato di StorageProvider.stream(); la cartella
    di lavoro viene rimossa quando la risposta è stata inviata. ``seekable``:
    lo storage ha ricevuto l'offset della richiesta Range."""
    cleanup = BackgroundTask(workspace.cleanup)
    if result.file:
        try:
            stored = workspace.file(result.file)
        except WorkspaceError:
            raise HTTPException(status_code=404, detail="File not found in storage")
        return FileResponse(
            path=stored.path,
            media_type=result.media_type,
            filename=result.filename,
            headers={"Accept-Ranges": "bytes"},
            background=cleanup,
        )
    if result.url:
        # Il tipo che lo storage dichiara vale quanto l'audio (anche
        # application/octet-stream, se è quello che dichiara).
        return _proxy_stream(
            result.url, provider=provider, accept=(*AUDIO_TYPES, result.media_type),
            headers=result.headers, background=cleanup, range_header=range_header,
        )
    if result.chunks is not None:
        return _chunks_response(result, cleanup, ranged=seekable and _range_start(range_header) is not None)
    raise HTTPException(status_code=404, detail="File not found in storage")


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

def _sync_stream(session, track_id: str, registry, auth_manager) -> tuple[str, str] | None:
    """Il sync provider che ha indicizzato la traccia e il suo URL di
    streaming. Un sync provider che fallisce si salta."""
    for sync in registry.all_sync():
        record = session.exec(
            select(TrackIdentifierRecord).where(
                TrackIdentifierRecord.track_id == track_id,
                TrackIdentifierRecord.id_type == sync_id_type(sync.name),
            )
        ).first()
        if record is None:
            continue
        try:
            url = sync.get_stream_url(record.value, auth_manager.authenticate(sync))
        except AuthError:
            continue
        except Exception as exc:  # noqa: BLE001 — un provider che fallisce non è un 500
            logger.warning("Sync provider %s gave no stream URL: %s", sync.name, exc)
            continue
        if url:
            return sync.name, url
    return None


def _storage_failure_kind(storage: StorageProvider, file_path: str, storage_session, exc: Exception) -> str:
    """Uno storage che non consegna un file o l'ha perso (404) o adesso non
    risponde (503): lo dice ``exists``, mai il testo dell'errore. Una
    connessione scaduta o rifiutata è già la risposta (non risponde): chi
    ascolta non aspetta un secondo tentativo per saperlo."""
    if isinstance(exc, ProviderCallFailed) and exc.kind in ("timeout", "unreachable"):
        return "unreachable"
    try:
        return "unreachable" if storage.exists(file_path, storage_session) else "not_found"
    except Exception:  # noqa: BLE001 — non risponde nemmeno a exists
        return "unreachable"


def _stream_stored(storage: StorageProvider, storage_session, file_path: str, config, range_header) -> Response:
    """Il file dallo storage; un fallimento dello storage è un
    :class:`ProviderCallError` (404 o 503), mai un 500."""
    workspace = new_workspace(config.workspace_dir, storage.name)
    # A storage that restarts a transfer at an offset gets the Range start
    # (a seek); the others hand the whole file and the host serves ranges.
    seekable = storage.supports_stream_offset
    offset = _range_start(range_header) if seekable else None
    try:
        try:
            if offset:
                result = storage.stream(file_path, str(workspace), storage_session, offset=offset)
            else:
                result = storage.stream(file_path, str(workspace), storage_session)
        except Exception as exc:  # noqa: BLE001 — qualunque errore del provider
            kind = _storage_failure_kind(storage, file_path, storage_session, exc)
            logger.warning("Storage %s could not stream %s (%s): %s", storage.name, file_path, kind, exc)
            raise ProviderCallError(storage.name, exc, "provider", kind=kind) from exc
        return _stored_file_response(result, workspace, range_header, provider=storage.name, seekable=seekable)
    except BaseException:
        workspace.cleanup()
        raise


@router.get("/stream/{track_id}")
def stream_track(
    track_id: str,
    request: Request,
    session: SessionDep,
    config: ConfigDep,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    cooldown: StreamCooldownDep,
    _stream_user: StreamUserDep,  # noqa: ARG001 — gate only, see routes/deps.get_stream_user
):
    """Stream a track of the library: its file on the storage provider
    (downloaded or found by the library scan, never one known missing), else
    the copy a sync provider indexed — the same sources that make a track
    "in library" (``db.library_store``).

    Full audio — gated by StreamUserDep so an anonymous caller gets 401 when
    auth is enabled (docs/permissions.md: an anonymous visitor may only hear
    the preview, never the stored/synced file).

    A storage that cannot hand the file over (lost, unreachable) falls back
    to the sync provider's copy; without one the storage failure is the
    answer (404 or 503), never a 500. Every step before the storage's first
    byte waits at most ``stream_start_timeout``, and a storage that did not
    answer rests (``storage_stream_cooldown_seconds``): meanwhile a track
    with a sync copy plays that copy at once."""
    range_header = request.headers.get("range")
    stored_failure: Exception | None = None
    file_path = next(iter(stored_file_paths(session, track_id)), None)
    storage = storage_session = None
    if file_path:
        storage, storage_session = _storage(registry, config, auth_manager)
        if storage is None:
            stored_failure = HTTPException(status_code=503, detail="No storage provider available")

    sync_stream = None
    sync_asked = False
    if storage is not None and cooldown.cooling(storage.name):
        sync_stream, sync_asked = _sync_stream(session, track_id, registry, auth_manager), True
        if sync_stream:
            logger.info("Track %s: storage %s is resting, streaming the copy of %s",
                        track_id, storage.name, sync_stream[0])
    if storage is not None and sync_stream is None:
        try:
            response = _stream_stored(storage, storage_session, file_path, config, range_header)
        except ProviderCallError as exc:
            stored_failure = exc
            if exc.kind == "unreachable":
                cooldown.record(storage.name, False)
        else:
            cooldown.record(storage.name, True)
            return response

    if not sync_asked:
        sync_stream = _sync_stream(session, track_id, registry, auth_manager)
    if sync_stream:
        sync_name, url = sync_stream
        if stored_failure is not None:
            logger.info("Track %s: storage unavailable, streaming the copy of %s", track_id, sync_name)
        return _proxy_stream(
            url, provider=sync_name, subject="sync_provider", accept=AUDIO_TYPES, range_header=range_header,
        )
    if stored_failure is not None:
        raise stored_failure
    raise HTTPException(status_code=404, detail="No file in the library for this track")


@router.get("/stream/provider/{provider_name}/{track_id}")
def stream_from_provider(
    provider_name: str,
    track_id: str,
    request: Request,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    _stream_user: StreamUserDep,  # noqa: ARG001 — gate only, see routes/deps.get_stream_user
):
    """Proxy stream from an authenticated provider (e.g. a Subsonic server).

    Full audio — gated by StreamUserDep, same as /stream/{track_id}."""
    sync = registry.get_sync_provider(provider_name)
    if not sync:
        raise HTTPException(status_code=404, detail=f"Sync provider '{provider_name}' not found")

    try:
        session = auth_manager.authenticate(sync)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    with provider_call(provider_name, "sync_provider"):
        stream_url = sync.get_stream_url(track_id, session)
    if not stream_url:
        raise HTTPException(status_code=404, detail="No stream URL available")

    return _proxy_stream(
        stream_url, provider=provider_name, subject="sync_provider", accept=AUDIO_TYPES,
        range_header=request.headers.get("range"),
    )


@router.get("/cover/provider/{provider_name}/{cover_id}")
def cover_from_provider(
    provider_name: str,
    cover_id: str,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
):
    """Proxy cover art from an authenticated provider (e.g. a Subsonic server).

    Only an image is served: anything else the provider answers (an error
    document with a 200) is a 404, remembered for MISSING_COVER_TTL_S."""
    provider = registry.get_metadata_provider(provider_name)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Metadata provider '{provider_name}' not found")
    if _cover_known_missing(provider_name, cover_id):
        raise ProviderCallError(provider_name, LookupError("cover recently found missing"), kind="not_found")

    try:
        session = auth_manager.authenticate(provider)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    with provider_call(provider_name):
        cover_url = provider.get_cover_url(cover_id, session)
    if not cover_url:
        raise HTTPException(status_code=404, detail="No cover URL available")

    try:
        return _proxy_stream(
            cover_url, provider=provider_name, subject="metadata_provider", accept=IMAGE_TYPES,
            unexpected_kind="not_found",
        )
    except ProviderCallError as exc:
        if exc.kind == "not_found":
            _remember_missing_cover(provider_name, cover_id)
        raise


@router.get("/preview/{provider_name}/{track_id}")
def preview_from_provider(
    provider_name: str,
    track_id: str,
    request: Request,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
):
    """Proxy the preview of a provider whose preview URLs are signed and
    expire: the URL is asked to the provider (``get_preview_url``) when the
    listen starts, so no cache ever serves an expired one. Public, like the
    previews themselves (a guest may listen to them)."""
    provider = registry.get_metadata_provider(provider_name)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Metadata provider '{provider_name}' not found")

    def proxy(url: str) -> StreamingResponse:
        return _proxy_stream(
            url, provider=provider_name, subject="metadata_provider", accept=AUDIO_TYPES,
            unexpected_kind="not_found", range_header=request.headers.get("range"),
        )

    recent = _recent_preview_url(provider_name, track_id)
    if recent:
        try:
            return proxy(recent)
        except ProviderCallError:
            _remember_preview_url(provider_name, track_id, None)

    try:
        session = auth_manager.authenticate(provider)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    with provider_call(provider_name):
        url = provider.get_preview_url(track_id, session)
    # A host URL would send the request back here.
    if not url or public_urls.is_host_served(url):
        raise HTTPException(status_code=404, detail="No preview available")
    response = proxy(url)
    _remember_preview_url(provider_name, track_id, url)
    return response


@router.get("/library/duplicates/members/{member_id}/stream")
def stream_duplicate_member(
    member_id: str,
    request: Request,
    session: SessionDep,
    config: ConfigDep,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    _admin: StreamAdminDep,  # noqa: ARG001 — gate only, see routes/deps.require_stream_admin
):
    """Stream ONE file of a duplicate group, so an admin can hear each copy
    before choosing the one to keep. The file is found by the member's own
    identity: its sync id on the sync provider whose library the scan read,
    else its real path on the storage (the Musizerr download it is linked
    to). Never ``/stream/{track_id}``, which picks a file of the track by
    itself.

    Admin only (the streaming cookie or a bearer token). 404 for an unknown
    member, a file already deleted, or a member no source can play; a source
    that fails answers like every provider failure (503/502), never a 500."""
    member = session.get(DuplicateMember, member_id)
    if member is None or member.deleted_at is not None:
        raise HTTPException(status_code=404, detail="No such duplicate member")
    range_header = request.headers.get("range")
    failure: Exception | None = None

    sync = registry.get_sync_provider(config.sync_provider) if config.sync_provider and member.sync_id else None
    if sync is not None:
        try:
            with provider_call(sync.name, "sync_provider"):
                url = sync.get_stream_url(member.sync_id, auth_manager.authenticate(sync))
            if url:
                return _proxy_stream(
                    url, provider=sync.name, subject="sync_provider", accept=AUDIO_TYPES, range_header=range_header,
                )
        except ProviderCallError as exc:
            failure = exc

    download = session.get(Download, member.download_id) if member.download_id else None
    if download is not None and download.file_path:
        storage, storage_session = _storage(registry, config, auth_manager)
        if storage is not None:
            if failure is not None:
                logger.info("Duplicate member %s: sync provider unavailable, streaming the stored file", member_id)
            return _stream_stored(storage, storage_session, download.file_path, config, range_header)
    if failure is not None:
        raise failure
    raise HTTPException(status_code=404, detail="No source can play this file")


def _streamable(session, track_id: str, registry, config) -> bool:
    """``/stream/{track_id}`` has a source: a stored file with a storage
    provider to read it, or a sync provider's copy. Decided on the database
    (no request to the storage), like the library itself."""
    if stored_file_paths(session, track_id) and resolve_storage_provider(registry, config.storage_provider)[0]:
        return True
    types = sync_id_types(registry)
    return bool(types) and session.exec(
        select(TrackIdentifierRecord.id).where(
            TrackIdentifierRecord.track_id == track_id, TrackIdentifierRecord.id_type.in_(types),
        )
    ).first() is not None


@router.get("/playback/{provider}/{provider_id}")
def get_playback_info(
    provider: str,
    provider_id: str,
    session: SessionDep,
    config: ConfigDep,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    user: OptionalUserDep,
):
    """
    Return playback info for a track.

    Full playback is available only if ``/stream/{track_id}`` has something
    to play — a file of the track the storage holds (one the library file
    reconciliation found missing does not count) or a sync provider's copy —
    AND (when auth is enabled) the caller is signed in — an anonymous visitor may only ever get the preview, never
    the full stream_url (docs/permissions.md). `/stream/{track_id}` itself is
    also gated (StreamUserDep) so this isn't the only line of defense, but
    the URL handed out here should already be honest about what the caller
    can actually reach.
    """
    id_type = f"{provider}_id" if provider not in ("isrc", "mbid") else provider

    tid = session.exec(
        select(TrackIdentifierRecord)
        .where(TrackIdentifierRecord.id_type == id_type, TrackIdentifierRecord.value == provider_id)
    ).first()

    can_reach_full = not config.auth_enabled or user is not None

    preview_url = None
    if tid:
        track_record = session.get(TrackRecord, tid.track_id)
        stored = track_data_dict(track_record) if track_record else None
        if stored is not None:
            preview_url = public_urls.publish(stored.get("preview_url"))

    if tid and can_reach_full and _streamable(session, tid.track_id, registry, config):
        return {"type": "full", "stream_url": public_urls.stored_stream_url(tid.track_id)}

    if preview_url:
        return {"type": "preview", "stream_url": preview_url}
    return {"type": "none", "stream_url": None}
