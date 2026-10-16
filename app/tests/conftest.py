"""
Fixture e fake condivisi per la suite backend.

Regola d'oro rispettata: i fake vivono qui (nei test), NON in ``core/``. Il core
resta provider-agnostico; i test lo esercitano con provider finti che
implementano i contratti astratti di ``musizerr_sdk.interfaces``. Nessun provider
reale compare in questi test.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
import time

import pytest

# The key of the stored credentials (docs/credentials.md) the whole suite runs with, the way it is
# given AUTH_SECRET: a fixed test key, FORCED so that a run where a real key is in the environment
# never uses it. A test about the keys sets its own (tests/credentials_support.use_keys).
from tests.credentials_support import KEY as _TEST_CREDENTIALS_KEY

os.environ["CREDENTIALS_KEY"] = _TEST_CREDENTIALS_KEY
os.environ.pop("CREDENTIALS_KEY_PREVIOUS", None)

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizerr_sdk.identifiers import Identifier
from musizerr_sdk.interfaces import (
    DownloadProvider,
    Identity,
    IdentificationProvider,
    MetadataProvider,
    StorageProvider,
    SyncCandidate,
    SyncProvider,
)
from musizerr_sdk.models import Album, Artist, LibraryItem, Track
from musizerr_sdk.context import StaticProviderContext
from tests.database import DIALECT, new_engine, release_engines
from tests import host_boundary
from tests.loopback import as_proxy


host_boundary.install()


# ------------------------------------------------------------------
# File audio
# ------------------------------------------------------------------

def write_flac(path, *, seconds: int = 3, **tags: str) -> Path:
    """Un FLAC valido senza audio (solo STREAMINFO e i tag dati), letto da
    mutagen come qualunque altro: nessun encoder necessario."""
    from mutagen.flac import FLAC

    rate = 44100
    # sample rate (20 bit), canali - 1 (3), bit per campione - 1 (5), campioni (36)
    packed = (rate << 44) | (0 << 41) | (15 << 36) | (rate * seconds)
    streaminfo = (4096).to_bytes(2, "big") * 2 + bytes(6) + packed.to_bytes(8, "big") + bytes(16)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    audio = FLAC(path)
    audio.add_tags()
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


# ------------------------------------------------------------------
# Redis
# ------------------------------------------------------------------

class FakeRedis:
    """The few Redis commands the workers use for their locks (get, set with
    ``nx``/``ex``, exists, delete). ``ex`` is honoured against ``clock``
    (seconds, ``time.monotonic`` unless a test drives virtual time)."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._store: dict[str, tuple[str, float | None]] = {}

    def _live(self, key):
        item = self._store.get(key)
        if item is not None and item[1] is not None and item[1] <= self._clock():
            del self._store[key]
            return None
        return item

    def get(self, key):
        item = self._live(key)
        return item[0].encode() if item else None

    def set(self, key, value, ex=None, nx=False):
        if nx and self._live(key) is not None:
            return None
        self._store[key] = (str(value), self._clock() + ex if ex else None)
        return True

    def incr(self, key):
        item = self._live(key)
        value = int(item[0]) + 1 if item else 1
        self._store[key] = (str(value), item[1] if item else None)
        return value

    def expire(self, key, seconds):
        item = self._live(key)
        if item is not None:
            self._store[key] = (item[0], self._clock() + seconds)
        return int(item is not None)

    def exists(self, key):
        return int(self._live(key) is not None)

    def ttl(self, key):
        item = self._live(key)
        if item is None:
            return -2
        return -1 if item[1] is None else max(0, int(item[1] - self._clock()))

    def delete(self, key):
        return int(self._store.pop(key, None) is not None)

    def expire_now(self, key):
        self._store.pop(key, None)


# ------------------------------------------------------------------
# Factory di modelli di dominio
# ------------------------------------------------------------------

def make_track(
    title: str = "Song",
    artist: str = "Artist",
    album: str = "Album",
    *,
    duration_ms: int = 200_000,
    provider: str = "fake",
    provider_id: str | None = None,
    isrc: str | None = None,
    mbid: str | None = None,
    year: int | None = None,
) -> Track:
    ids: list[Identifier] = []
    if isrc:
        ids.append(Identifier(type="isrc", value=isrc))
    if mbid:
        ids.append(Identifier(type="mbid", value=mbid))
    return Track(
        title=title,
        artist=artist,
        album=album,
        duration_ms=duration_ms,
        provider=provider,
        provider_id=provider_id or f"{provider}-{title}",
        external_ids=ids,
        year=year,
    )


def make_album(
    title: str = "Album",
    artist: str = "Artist",
    *,
    provider: str = "fake",
    provider_id: str | None = None,
    year: int | None = 2020,
    mbid: str | None = None,
) -> Album:
    ids: list[Identifier] = []
    if mbid:
        ids.append(Identifier(type="mbid", value=mbid))
    return Album(
        title=title,
        artist=artist,
        year=year,
        provider=provider,
        provider_id=provider_id or f"{provider}-{title}",
        external_ids=ids,
    )


def make_library_item(
    title: str = "Song",
    artist: str = "Artist",
    album: str = "Album",
    *,
    sync_id: str | None = None,
    path: str | None = None,
    year: int | None = None,
    duration_ms: int = 200_000,
    size: int = 5_000_000,
    suffix: str = "flac",
    bitrate: int = 1000,
    sample_rate: int = 44100,
) -> LibraryItem:
    ident = sync_id or f"sync-{title}-{path or 'x'}"
    return LibraryItem(
        sync_id=ident,
        path=path or f"{artist}/{album}/{title}.{suffix}",
        title=title,
        artist=artist,
        album=album,
        year=year,
        duration_ms=duration_ms,
        size=size,
        suffix=suffix,
        bitrate=bitrate,
        sample_rate=sample_rate,
    )


# Prefisso usato da FakeIdentificationProvider per "leggere" l'identità dal
# contenuto stesso del file finto invece di tenere una mappa a parte keyed su
# un path imprevedibile (il file che SyncProvider.fetch_partial scrive nella
# cartella di lavoro) — vedi encode_identity_bytes/
# FakeSyncProvider sotto: lo stesso schema del resto della suite, nessun
# provider reale.
_IDENTITY_PREFIX = "IDENTITY:"


def encode_identity_bytes(
    recording_id: str, *, title: str = "Song", artist: str = "Artist", score: float = 0.95,
) -> bytes:
    """Contenuto finto di un file audio che codifica la sua identità 'vera'
    (per il fingerprint): FakeIdentificationProvider.identify() lo decodifica
    leggendo il file che FakeSyncProvider.fetch_partial ha prodotto."""
    return f"{_IDENTITY_PREFIX}{recording_id}:{title}:{artist}:{score}".encode()


def make_artist(
    name: str = "Artist",
    *,
    provider: str = "fake",
    provider_id: str | None = None,
    mbid: str | None = None,
) -> Artist:
    ids: list[Identifier] = []
    if mbid:
        ids.append(Identifier(type="mbid", value=mbid))
    return Artist(
        name=name,
        provider=provider,
        provider_id=provider_id or f"{provider}-{name}",
        external_ids=ids,
    )


# ------------------------------------------------------------------
# Fake provider
# ------------------------------------------------------------------

class FakeMetadataProvider(MetadataProvider):
    """Metadata provider deterministico per i test dell'aggregatore."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(
        self,
        name: str,
        *,
        tracks: list[Track] | None = None,
        albums: list[Album] | None = None,
        artists: list[Artist] | None = None,
        delay: float = 0.0,
        fail: bool = False,
    ) -> None:
        super().__init__(StaticProviderContext())
        self.name = name
        self.label = name.title()
        self._tracks = tracks or []
        self._albums = albums or []
        self._artists = artists or []
        self._delay = delay
        self._fail = fail
        # Thread da cui è stato eseguito ogni search — per verificare la
        # parallelizzazione e che l'auth non avvenga dentro i thread.
        self.search_threads: list[int] = []

    # --- BaseProvider (astratti) ---
    def config_schema(self):
        return []

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session) -> str | None:
        return None

    # --- ricerca ---
    def _run(self, items: list):
        self.search_threads.append(threading.get_ident())
        if self._fail:
            raise RuntimeError(f"{self.name} boom")
        if self._delay:
            time.sleep(self._delay)
        return list(items)

    def search_tracks(self, query, session, limit: int = 20):
        return self._run(self._tracks)[:limit]

    def search_albums(self, query, session, limit: int = 20):
        return self._run(self._albums)[:limit]

    def search_artists(self, query, session, limit: int = 20):
        return self._run(self._artists)[:limit]


class FakeSyncProvider(SyncProvider):
    """Sync provider finto: 'disponibili' le tracce il cui titolo è noto."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(
        self,
        name: str = "fakesync",
        available: dict[str, str] | None = None,
        *,
        library: list[LibraryItem] | None = None,
        files: dict[str, bytes] | None = None,
    ) -> None:
        super().__init__(StaticProviderContext())
        self.name = name
        self.label = name.title()
        # mappa titolo-normalizzato-ish → sync_id
        self._available = available or {}
        self._library = library or []
        # sync_id -> bytes finti del file, per fetch_partial (mai per path:
        # il fetch reale va per sync_id, vedi musizerr_sdk.interfaces.SyncProvider).
        self.files: dict[str, bytes] = files or {}
        self.availability_calls = 0
        self.scan_calls = 0
        self.liked: list[str] = []
        self.fetch_partial_calls: list[str] = []

    def config_schema(self):
        return []

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session) -> str | None:
        return None

    def trigger_scan(self, session) -> None:
        self.scan_calls += 1

    def find_candidates(self, track, session):
        self.availability_calls += 1
        sync_id = self._available.get(track.title.lower())
        return [SyncCandidate(sync_id=sync_id, track=track)] if sync_id else []

    def get_stream_url(self, sync_id, session) -> str | None:
        return f"http://sync/{sync_id}"

    def like_track(self, sync_id, session) -> None:
        self.liked.append(sync_id)

    def list_library(self, session) -> list[LibraryItem]:
        return list(self._library)

    def fetch_partial(self, sync_id: str, workspace: str, session, max_bytes: int | None = None) -> str | None:
        self.fetch_partial_calls.append(sync_id)
        data = self.files.get(sync_id)
        if data is None:
            return None
        if max_bytes is not None:
            data = data[:max_bytes]
        (Path(workspace) / "partial").write_bytes(data)
        return "partial"


class FakeDownloadProvider(DownloadProvider):
    """Download provider finto minimale: implementa solo gli astratti del
    contratto. Le capability opzionali (delegation_event_handle,
    register_push_endpoint, ...) NON sono overridate qui di proposito, cosi'
    i test possono esercitare i default del contratto (return None) —
    vedi FakeDelegatingDownloadProvider sotto per un provider che le implementa.
    """

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name: str = "fakedl") -> None:
        super().__init__(StaticProviderContext())
        self.name = name
        self.label = name.title()

    def config_schema(self):
        return []

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session) -> str | None:
        return None

    def download_by_id(self, identifier, workspace, session):
        return None


class FakeDelegatingDownloadProvider(FakeDownloadProvider):
    """Download provider finto che delega a un sistema esterno: implementa le
    due capability opzionali di push (musizerr_sdk.interfaces) con un vocabolario
    di eventi/payload inventato, per non far dipendere i test da un provider
    reale."""

    def __init__(self, name: str = "fakedelegating", *, register_outcome: str | None = "created") -> None:
        super().__init__(name)
        self._register_outcome = register_outcome
        self.registered_urls: list[str] = []

    def delegation_event_handle(self, payload: dict) -> str | None:
        if not isinstance(payload, dict):
            return None
        if payload.get("event") != "done":
            return None
        job_id = payload.get("job_id")
        return str(job_id) if job_id is not None else None

    def register_push_endpoint(self, callback_url: str, session) -> str | None:
        self.registered_urls.append(callback_url)
        return self._register_outcome


class FakeIdentificationProvider(IdentificationProvider):
    """Identification provider finto — usato per verificare che GET
    /providers (routes/config.py::list_providers) copra OGNI categoria che
    il registry conosce, ``identification`` compresa: prima di Fase 5a fix 2
    questa categoria aveva impostazioni proprie nello schema unificato
    (``merged_schema``) ma non compariva affatto in GET /providers, quindi un
    provider come questo non aveva un'etichetta trovabile lato UI.

    ``config_valid``/``available`` sono regolabili (default: entrambi True,
    il caso "tutto ok") per riprodurre anche il caso di un provider con
    ``auth.flow == NONE`` e config incompleta o non disponibile — quello che
    Fase 5b fix 4 (``_provider_info``) deve segnalare come non ``usable``
    pur restando ``authenticated`` (nessun login da tentare)."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name: str = "fakeident", *, config_valid: bool = True, available: bool = True) -> None:
        super().__init__(StaticProviderContext())
        self.name = name
        self.label = name.title()
        self._config_valid = config_valid
        self._available = available
        # Fase 5c: conta le chiamate a identify() — usato per dimostrare che
        # un gate a monte (es. il toggle globale identification_enabled in
        # workers/tasks.py::_should_run_anti_mislabel) impedisce DAVVERO la
        # chiamata, non solo che il chiamante "non fa nulla di visibile".
        self.identify_calls = 0

    def config_schema(self):
        return []

    def validate_config(self, config: dict) -> bool:
        return self._config_valid

    def is_available(self) -> bool:
        return self._available

    def test_connection(self, session) -> str | None:
        return None

    def identify(self, audio):
        self.identify_calls += 1
        # Decodifica l'identità 'vera' dal contenuto del file finto (vedi
        # encode_identity_bytes sopra) — se il file non esiste o non porta il
        # prefisso atteso, nessun match: comportamento identico a prima per
        # ogni test che passa un path fittizio/inesistente.
        try:
            with open(audio.path, "rb") as f:
                content = f.read()
        except OSError:
            return None
        text = content.decode("utf-8", errors="ignore")
        if not text.startswith(_IDENTITY_PREFIX):
            return None
        try:
            _, recording_id, title, artist, score = text.split(":", 4)
            return Identity(title=title, artist=artist, score=float(score), recording_id=recording_id)
        except ValueError:
            return None


class FakeStorageProvider(StorageProvider):
    """Storage provider finto, in-memory: ``files`` tiene relative_path ->
    bytes. Usato per la RISOLUZIONE dei duplicati (cancellazione del path
    REALE via ``delete``) — il fetch per il fingerprinting passa dal sync
    provider (``FakeSyncProvider.fetch_partial``), mai da qui."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name: str = "fakestorage") -> None:
        super().__init__(StaticProviderContext())
        self.name = name
        self.label = name.title()
        self.files: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def config_schema(self):
        return []

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session) -> str | None:
        return None

    def store(self, source, relative_dest: str, session) -> str:
        with open(source.path, "rb") as f:
            self.files[relative_dest] = f.read()
        return relative_dest

    def exists(self, relative_path: str, session) -> bool:
        return relative_path in self.files

    def stream(self, relative_path: str, workspace: str, session):
        raise NotImplementedError("not needed by duplicate-detection tests")

    def delete(self, relative_path: str, session) -> bool:
        self.deleted.append(relative_path)
        return self.files.pop(relative_path, None) is not None


class FakeAuthManager:
    """Restituisce sessioni opache e traccia i thread da cui viene invocato."""

    def __init__(self) -> None:
        self.auth_calls = 0
        self.auth_threads: list[int] = []
        self._lock = threading.Lock()

    def authenticate(self, provider):
        with self._lock:
            self.auth_calls += 1
            self.auth_threads.append(threading.get_ident())
        return AuthSession.anonymous()


class FakeRegistry:
    """Registry fittizio generico, usato dai test HTTP/service (non solo
    dall'aggregatore di ricerca): implementa l'intero Protocol
    ``core.interfaces.ProviderRegistry`` cosi' da poter alimentare anche
    ``core.settings_resolver.merged_schema`` e ``services.settings``.
    Le categorie non passate restituiscono semplicemente liste vuote — nessun
    provider reale compare mai qui.

    I provider passati sono quelli del test (il lato "provider", su cui si
    asserisce cosa gli è stato chiesto); l'host riceve i loro proxy loopback
    (``tests/loopback.py``): non tiene mai l'istanza."""

    def __init__(
        self,
        metadata: list[MetadataProvider] | None = None,
        syncs: list[SyncProvider] | None = None,
        downloads: list[DownloadProvider] | None = None,
        lyrics: list | None = None,
        storage: list | None = None,
        identification: list | None = None,
    ) -> None:
        self._metadata = [as_proxy(p) for p in metadata or []]
        self._syncs = [as_proxy(p) for p in syncs or []]
        self._downloads = [as_proxy(p) for p in downloads or []]
        self._lyrics = [as_proxy(p) for p in lyrics or []]
        self._storage = [as_proxy(p) for p in storage or []]
        self._identification = [as_proxy(p) for p in identification or []]

    def rejected(self):
        return {}

    def all_metadata(self):
        return list(self._metadata)

    def all_sync(self):
        return list(self._syncs)

    def all_download(self):
        return list(self._downloads)

    def all_lyrics(self):
        return list(self._lyrics)

    def all_storage(self):
        return list(self._storage)

    def all_identification(self):
        return list(self._identification)

    def get_metadata_provider(self, name: str):
        return next((p for p in self._metadata if p.name == name), None)

    def get_download_provider(self, name: str):
        return next((p for p in self._downloads if p.name == name), None)

    def get_lyrics_provider(self, name: str):
        return next((p for p in self._lyrics if p.name == name), None)

    def get_storage_provider(self, name: str):
        return next((p for p in self._storage if p.name == name), None)

    def get_sync_provider(self, name: str):
        return next((p for p in self._syncs if p.name == name), None)

    def get_identification_provider(self, name: str):
        return next((p for p in self._identification if p.name == name), None)

    def metadata_names(self) -> list[str]:
        return [p.name for p in self._metadata]

    def download_names(self) -> list[str]:
        return [p.name for p in self._downloads]

    def names_for_category(self, category: str) -> list[str]:
        by_category = {
            "metadata": self._metadata,
            "download": self._downloads,
            "lyrics": self._lyrics,
            "storage": self._storage,
            "sync": self._syncs,
            "identification": self._identification,
        }
        return [p.name for p in by_category.get(category, [])]


# ------------------------------------------------------------------
# Fixture
# ------------------------------------------------------------------

@pytest.fixture
def auth_manager() -> FakeAuthManager:
    return FakeAuthManager()


@pytest.fixture(autouse=True)
def _clear_availability_cache():
    """Il cache di availability è globale al modulo: azzera prima di ogni test."""
    try:
        from services import enrichment

        with enrichment._avail_lock:
            enrichment._avail_cache.clear()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _workspace_dir_in_tmp(tmp_path, monkeypatch):
    """Le cartelle di lavoro dell'host (ADR-0003) finiscono sotto la cartella
    temporanea del test, mai sul volume reale indicato dal default."""
    from services.config_manager import ConfigManager

    root = tmp_path / "workspaces"
    monkeypatch.setattr(ConfigManager, "workspace_dir", property(lambda self: root))
    return root


@pytest.fixture(autouse=True)
def _credential_warnings_start_empty():
    """A value that cannot be read is reported once per process: each test starts with none reported."""
    from db import credential_cipher

    credential_cipher.forget_reports()


@pytest.fixture(autouse=True)
def _fast_password_hashing(monkeypatch):
    """bcrypt at its minimum cost: the production cost (~0.17 s per hash,
    and again per check) is the point in production and only slows the
    tests that create users or sign in. Same algorithm and format: a hash
    made here verifies exactly like a production one."""
    import bcrypt

    gensalt = bcrypt.gensalt
    monkeypatch.setattr(bcrypt, "gensalt", lambda rounds=4, prefix=b"2b": gensalt(rounds, prefix))


@pytest.fixture(autouse=True)
def _login_rate_limit_counter(monkeypatch):
    """No Redis in the tests: the counter of the sign-in routes is a fresh fake per test."""
    from services import login_rate_limit

    redis = FakeRedis()
    monkeypatch.setattr(login_rate_limit, "_client", lambda: redis)
    return redis


@pytest.fixture(autouse=True)
def sent_tasks(monkeypatch):
    """No broker in the tests, ever: every task the host sends
    (``celery.send_task``, and ``apply_async``/``delay``, which go through
    it) is recorded here as ``(task_name, kwargs, options)``. Without this a
    test reaching a dispatch waits for kombu's reconnection backoff to a
    Redis that is not there (~19 s each, and a dependency on the network).
    A test that checks the dispatch itself patches ``send_task`` again."""
    from types import SimpleNamespace
    from uuid import uuid4

    from workers.celery_app import celery

    calls: list[tuple[str, dict, dict]] = []

    def _send_task(name, args=None, kwargs=None, **options):
        calls.append((name, kwargs or {}, options))
        return SimpleNamespace(id=options.get("task_id") or str(uuid4()))

    monkeypatch.setattr(celery, "send_task", _send_task)
    return calls


@pytest.fixture(autouse=True)
def dispatched_tasks(monkeypatch):
    """No broker in the tests: the tasks the favorites service queues are
    recorded here instead (``(task_name, kwargs)``)."""
    from services import favorites

    from types import SimpleNamespace

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(favorites, "dispatch", lambda name, **kwargs: calls.append((name, kwargs)))
    # No sync provider unless a test sets one (monkeypatch favorites.get_config).
    monkeypatch.setattr(favorites, "get_config", lambda: SimpleNamespace(sync_provider=None))
    return calls


@pytest.fixture(autouse=True)
def request_events(monkeypatch):
    """No Redis in the tests: the events of download requests (the progress
    stream) are recorded here instead."""
    from services import download_requests

    events: list[dict] = []
    monkeypatch.setattr(download_requests, "publish_event", events.append)
    return events


# ------------------------------------------------------------------
# Database: one clean database per call, on the dialect under test
# (tests/database.py)
# ------------------------------------------------------------------

_DIALECT_MARKERS = {"sqlite_only": "sqlite", "postgresql_only": "postgresql"}


def pytest_collection_modifyitems(config, items):
    """A test about one database engine is skipped, with its reason, on the other."""
    for item in items:
        for marker_name, dialect in _DIALECT_MARKERS.items():
            marker = item.get_closest_marker(marker_name)
            if marker is not None and DIALECT != dialect:
                item.add_marker(pytest.mark.skip(reason=f"{marker_name}: {marker.args[0]}"))


@pytest.fixture
def engine():
    """A clean database with every table of the models."""
    return new_engine()


@pytest.fixture(autouse=True)
def _release_databases():
    """The databases ``new_engine`` handed out to the test are dropped."""
    yield
    release_engines()
