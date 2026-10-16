"""
Le risposte reali delle rotte di lettura principali rispettano gli schemi di
``app/openapi.yaml``: ogni stato restituito è documentato, e il corpo è
validato contro lo schema dello spec — tipi, enum, campi obbligatori, e
nessun campo non documentato (un oggetto con ``properties`` è chiuso, salvo
``additionalProperties`` esplicito). Le risposte vengono dall'app vera
(``main.app``) alimentata dai provider finti, per guest, utente e admin.

Complementare a ``test_openapi_spec.py``, che verifica solo la presenza
delle rotte.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import Session

import main
from musizerr_sdk.auth import (
    AccountModel,
    AccountDisplayField,
    AuthDefinition,
    AuthFlow,
    ExternalUtilityConfig,
)
from musizerr_sdk.identifiers import Identifier
from musizerr_sdk.interfaces import LyricsProvider
from musizerr_sdk.models import Album, Artist, Lyrics, Playlist, TracksUnavailable
from musizerr_sdk.context import StaticProviderContext
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import (
    Download,
    DuplicateGroup,
    DuplicateMember,
    LibraryJobRun,
    RoutineRun,
    TrackRecord,
    User,
    UserFavorite,
    UserSavedTrack,
    UserSavedTrackProvider,
)
from db.track_store import stored_track_data
from routes import deps
from services.config_manager import ConfigManager

from tests.conftest import (
    FakeAuthManager,
    FakeDownloadProvider,
    FakeIdentificationProvider,
    FakeRegistry,
    FakeStorageProvider,
    FakeSyncProvider,
    make_track,
)
from tests.database import new_engine
from tests.test_public_urls import _ProxiedCatalog

SPEC_PATH = Path(main.__file__).resolve().parent / "openapi.yaml"
SPEC = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Validatore: sottoinsieme di JSON Schema 2020-12 usato dallo spec (OpenAPI
# 3.1), in modalità stretta.
# ---------------------------------------------------------------------------

_JSON_TYPES = {
    "null": lambda v: v is None,
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def _resolve(ref: str) -> dict:
    assert ref.startswith("#/"), ref
    node = SPEC
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _declared_properties(schema: dict) -> tuple[set[str], bool]:
    """Nomi dichiarati da uno schema oggetto, seguendo ``$ref`` e ``allOf``
    (come ``unevaluatedProperties: false``), e se ammette campi liberi."""
    if "$ref" in schema:
        names, free = _declared_properties(_resolve(schema["$ref"]))
    else:
        names, free = set(), False
    names |= set(schema.get("properties", {}))
    free = free or "additionalProperties" in schema and schema["additionalProperties"] is not False
    for sub in schema.get("allOf", []):
        sub_names, sub_free = _declared_properties(sub)
        names |= sub_names
        free = free or sub_free
    return names, free


def _errors(value, schema: dict, where: str = "$", closed: bool = True) -> list[str]:
    """Errori di *value* rispetto a *schema*. ``closed``: i campi non
    dichiarati (anche nei rami ``allOf``) sono un errore."""
    errors: list[str] = []
    if "nullable" in schema:
        errors.append(f"{where}: 'nullable' non è OpenAPI 3.1 (usa type: [..., 'null'])")
    if "$ref" in schema:
        errors += _errors(value, _resolve(schema["$ref"]), where, closed=False)
    for sub in schema.get("allOf", []):
        errors += _errors(value, sub, where, closed=False)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            passing = [s for s in schema[key] if not _errors(value, s, where, closed)]
            if not passing or (key == "oneOf" and len(passing) > 1):
                detail = [e for s in schema[key] for e in _errors(value, s, where, closed)][:6]
                errors.append(f"{where}: {len(passing)} rami di {key} validi ({detail})")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: {value!r} != const {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} fuori da enum {schema['enum']}")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_JSON_TYPES[t](value) for t in types):
            errors.append(f"{where}: {type(value).__name__} {value!r:.60} non è {types}")
            return errors
    if isinstance(value, dict):
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        is_object_schema = "properties" in schema or "object" in (
            schema["type"] if isinstance(schema.get("type"), list) else [schema.get("type")])
        if is_object_schema and "properties" not in schema and extra is None:
            errors.append(f"{where}: oggetto senza schema (né properties né additionalProperties)")
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{where}: manca il campo obbligatorio {name!r}")
        for name, item in value.items():
            if name in props:
                errors += _errors(item, props[name], f"{where}.{name}")
            elif isinstance(extra, dict):
                errors += _errors(item, extra, f"{where}.{name}")
        if closed and (is_object_schema or "$ref" in schema or "allOf" in schema):
            declared, free = _declared_properties(schema)
            if not free:
                errors += [f"{where}: campo non documentato {name!r}" for name in value if name not in declared]
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += _errors(item, schema["items"], f"{where}[{index}]")
    return errors


def _operation(method: str, path: str) -> dict:
    return SPEC["paths"][path][method.lower()]


def _response_schema(method: str, path: str, status: int) -> dict | None:
    responses = _operation(method, path)["responses"]
    assert str(status) in responses, f"{method} {path}: stato {status} non documentato ({sorted(responses)})"
    response = responses[str(status)]
    if "$ref" in response:
        response = _resolve(response["$ref"])
    content = response.get("content", {}).get("application/json")
    return content["schema"] if content else None


def assert_conforms(resp, method: str, path: str) -> None:
    schema = _response_schema(method, path, resp.status_code)
    if schema is None:
        return
    errors = _errors(resp.json(), schema)
    assert errors == [], f"{method} {path} -> {resp.status_code}:\n" + "\n".join(errors[:25])


def test_the_validator_rejects_what_it_should():
    track = {"$ref": "#/components/schemas/Track"}
    ok = {"kind": "track", "internal_id": "x", "title": "t", "artist": "a", "album": "b",
          "duration_ms": 1, "provider": "p", "provider_id": "1"}

    assert _errors(ok, track) == []
    assert _errors({**ok, "surprise": 1}, track)                  # campo non documentato
    assert _errors({k: v for k, v in ok.items() if k != "title"}, track)  # required
    assert _errors({**ok, "kind": "album"}, track)                 # enum
    assert _errors({**ok, "duration_ms": "1"}, track)              # tipo
    assert _errors({**ok, "year": None}, track) == []              # nullable 3.1


# ---------------------------------------------------------------------------
# Provider finti
# ---------------------------------------------------------------------------

CDN_COVER = "https://cdn.example.com/cover.jpg"


class _Catalog(_ProxiedCatalog):
    """Un catalogo completo: ogni metodo di lettura del contratto."""

    name = "catalog"
    label = "Catalog"

    def _track(self):
        track = super()._track()
        track.external_ids = [Identifier("isrc", "ISRC1"), Identifier("provider_id", "t1", self.name)]
        track.year = 2001
        track.track_number = 1
        track.album_provider_id = "a1"
        track.artist_provider_id = "ar1"
        return track

    def get_track(self, provider_id, session):
        return self._track() if provider_id == "t1" else None

    def get_album(self, provider_id, session):
        if provider_id != "a1":
            return None
        album = super().get_album(provider_id, session)
        album.year, album.album_type, album.track_count = 2001, "album", 1
        album.artist_provider_id = "ar1"
        return album

    def get_artist(self, provider_id, session):
        return super().get_artist(provider_id, session) if provider_id == "ar1" else None

    def get_artist_albums(self, provider_id, session, limit=50):
        # The album and its deluxe edition: one release with two editions.
        deluxe = Album(title="Album (Deluxe)", artist="Artist", provider=self.name, provider_id="a1d",
                       year=2003, album_type="album", track_count=2)
        return [self.get_album("a1", session), deluxe]

    def get_artist_top_tracks(self, provider_id, session, limit=10):
        return [self._track()]

    def get_playlist(self, provider_id, session):
        if provider_id == "p1":
            return super().get_playlist(provider_id, session)
        if provider_id == "locked":
            return Playlist(name="Locked", provider=self.name, provider_id="locked",
                            tracks_unavailable=TracksUnavailable(code="access_restricted", message="no"))
        return None

    def find_related_tracks(self, track, session, limit=20):
        other = make_track("Other Song", "Artist", provider=self.name, provider_id="t2", isrc="ISRC1")
        other.album, other.album_provider_id, other.year = "Compilation", "a2", 2005
        return [other]

    def supports_isrc_lookup(self):
        return True

    def find_by_isrc(self, isrc, session):
        return self._track() if isrc == "ISRC1" else None

    def search_tracks(self, query, session, limit=20):
        return [self._track()]

    def search_albums(self, query, session, limit=20):
        return [self.get_album("a1", session)]

    def search_artists(self, query, session, limit=20):
        return [self.get_artist("ar1", session)]

    def search_playlists(self, query, session, limit=20):
        return [self.get_playlist("p1", session), self.get_playlist("locked", session)]


class _Lyrics(LyricsProvider):
    name = "lyricsfake"
    label = "Lyrics"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self):
        super().__init__(StaticProviderContext())

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_lyrics(self, track, session):
        return Lyrics(plain="la la", source=self.name)


class _UtilityDownload(FakeDownloadProvider):
    """Download provider con login da utility esterna: ogni campo opzionale
    di ``_provider_info`` compare."""

    auth = AuthDefinition(flow=AuthFlow.EXTERNAL_UTILITY, account_model=AccountModel.SYSTEM, external_utility=ExternalUtilityConfig(
        credential_type="blob",
        borrow_oauth_client_from="catalog",
        library_package="some-lib",
        library_url="https://example.com/lib",
        disclaimer="Use at your own risk",
        setup_command="some-tool login",
        setup_instructions="Run the tool",
        account_display_fields=[AccountDisplayField(key="region", label="Region")],
    ))

    def is_library_available(self):
        return False


class _Sync(FakeSyncProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)


def _registry() -> FakeRegistry:
    return FakeRegistry(
        metadata=[_Catalog()],
        syncs=[_Sync("fakesync", {"song": "sync-1"})],
        downloads=[_UtilityDownload("utildl")],
        lyrics=[_Lyrics()],
        storage=[FakeStorageProvider()],
        identification=[FakeIdentificationProvider()],
    )


# ---------------------------------------------------------------------------
# App reale con DB in memoria
# ---------------------------------------------------------------------------


def _seed(engine) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        users = {role: User(username=role, password_hash="x", role=role) for role in ("user", "admin")}
        session.add_all(users.values())
        session.commit()
        track = make_track("Song", "Artist", provider="catalog", provider_id="t1", isrc="ISRC1")
        track.cover_url = "/cover/provider/catalog/cover-t1"
        track.stream_url = "/stream/provider/fakesync/sync-1"
        record = TrackRecord(title="Song", artist="Artist", album="Album", track_data=stored_track_data(track))
        failed = TrackRecord(title="Gone", artist="Artist", album="Album", track_data=None)
        elsewhere = TrackRecord(title="Elsewhere", artist="Other", album="Far", track_data=None)
        session.add_all([record, failed, elsewhere])
        session.commit()
        for user in users.values():
            session.add(UserFavorite(user_id=user.id, track_id=record.internal_id, source="heart"))
            liked = UserSavedTrack(user_id=user.id, track_id=elsewhere.internal_id)
            session.add(liked)
            session.flush()
            session.add(UserSavedTrackProvider(saved_track_id=liked.id, provider="catalog", provider_track_id="e1",
                                               added_at=now))
            saved = UserSavedTrack(user_id=user.id, track_id=record.internal_id)
            session.add(saved)
            session.flush()
            session.add(UserSavedTrackProvider(saved_track_id=saved.id, provider="catalog", provider_track_id="t1",
                                               added_at=now, stream_url="/stream/provider/fakesync/sync-1"))
            session.add(Download(track_id=record.internal_id, requested_by=user.id, status="downloaded",
                                 provider_used="utildl", file_path="Artist/Song.flac", started_at=now,
                                 completed_at=now))
            session.add(Download(track_id=failed.internal_id, requested_by=user.id, status="failed",
                                 error_message="ConnectError: [Errno 111] Connection refused", started_at=now))
        providers = json.dumps([
            {"provider": "catalog", "status": "failed", "processed": 0, "total": None, "count": 0,
             "error_code": "metadata_provider_unreachable", "error_message": "The provider could not be reached.",
             "error_detail": "ConnectError: [Errno 111] Connection refused"},
        ])
        for role in ("user", "admin"):
            session.add(LibraryJobRun(user_id=users[role].id, kind="library_sync", status="failed",
                                      providers=providers, result=json.dumps({"tracks_synced": 0}),
                                      error_code="metadata_provider_unreachable",
                                      error_message="The provider could not be reached.",
                                      error_detail="ConnectError: [Errno 111] Connection refused",
                                      started_at=now, completed_at=now))
            session.add(LibraryJobRun(user_id=users[role].id, kind="download_sync", status="running",
                                      params=json.dumps({"time_range": "all"}), phase="checking",
                                      processed=3, total=10, started_at=now, heartbeat_at=now))
        session.add(RoutineRun(routine_key="sync_saved_tracks", status="error", detail="boom",
                               started_at=now, finished_at=now))
        group = DuplicateGroup(recording_id="rec-1", reason="same recording")
        session.add(group)
        session.flush()
        session.add_all([
            DuplicateMember(group_id=group.id, sync_id="s1", path="A/1.flac", title="Song", artist="Artist",
                            suffix="flac", size=1, lossless=True, quality_known=True, fingerprint_score=0.97,
                            is_keeper=True),
            DuplicateMember(group_id=group.id, sync_id="s2", path="A/1.mp3", title="Song", artist="Artist",
                            suffix="mp3", size=1, lossless=False, quality_known=True, origin="unknown"),
        ])
        session.commit()
        return {role: create_access_token(u.id, u.role) for role, u in users.items()}


@pytest.fixture
def api(monkeypatch):
    engine = new_engine()
    tokens = _seed(engine)
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True, "metadata_provider_priority": ["catalog"]})
    registry = _registry()

    def _session():
        with Session(engine) as s:
            yield s

    overrides = {
        get_session: _session,
        deps.get_config: lambda: config,
        deps.get_registry: lambda: registry,
        deps.get_auth_manager: lambda: FakeAuthManager(),
    }
    main.app.dependency_overrides.update(overrides)
    from routes import discover
    with discover._cache_lock:
        discover._cache.clear()
    try:
        yield _Api(TestClient(main.app), tokens)
    finally:
        for key in overrides:
            main.app.dependency_overrides.pop(key, None)


class _Api:
    def __init__(self, client: TestClient, tokens: dict[str, str]):
        self.client = client
        self.tokens = tokens

    def get(self, url: str, role: str = "guest"):
        headers = {"Authorization": f"Bearer {self.tokens[role]}"} if role in self.tokens else {}
        return self.client.get(url, headers=headers)


# (url, route documentata, ruolo, stato atteso)
_READS = [
    *[(f"/search?q=song&type={kind}", "/search", "user", 200) for kind in ("track", "album", "artist", "playlist")],
    ("/search?q=song", "/search", "guest", 401),
    ("/search?q=%20", "/search", "user", 400),
    ("/track/catalog/t1", "/track/{provider}/{id}", "guest", 200),
    ("/track/catalog/nope", "/track/{provider}/{id}", "guest", 404),
    ("/track/unknown/t1", "/track/{provider}/{id}", "guest", 404),
    ("/track/catalog/t1/related", "/track/{provider}/{id}/related", "guest", 200),
    ("/track/catalog/nope/related", "/track/{provider}/{id}/related", "guest", 404),
    ("/track/catalog/t1/appears-on", "/track/{provider}/{id}/appears-on", "guest", 200),
    ("/track/catalog/nope/appears-on", "/track/{provider}/{id}/appears-on", "guest", 404),
    ("/track/catalog/t1/lyrics", "/track/{provider}/{id}/lyrics", "guest", 200),
    ("/album/catalog/a1", "/album/{provider}/{id}", "guest", 200),
    ("/album/catalog/nope", "/album/{provider}/{id}", "guest", 404),
    ("/album/catalog/a1/editions", "/album/{provider}/{id}/editions", "guest", 200),
    ("/album/catalog/nope/editions", "/album/{provider}/{id}/editions", "guest", 404),
    ("/artist/catalog/ar1", "/artist/{provider}/{id}", "guest", 200),
    ("/artist/catalog/nope", "/artist/{provider}/{id}", "guest", 404),
    ("/artist/catalog/ar1/albums", "/artist/{provider}/{id}/albums", "guest", 200),
    ("/artist/catalog/ar1/albums?page=1&limit=1", "/artist/{provider}/{id}/albums", "guest", 200),
    ("/artist/catalog/ar1/top-tracks", "/artist/{provider}/{id}/top-tracks", "guest", 200),
    ("/artist/catalog/nope/top-tracks", "/artist/{provider}/{id}/top-tracks", "guest", 404),
    ("/playlist/catalog/p1", "/playlist/{provider}/{id}", "guest", 200),
    ("/playlist/catalog/locked", "/playlist/{provider}/{id}", "guest", 200),
    ("/playlist/catalog/nope", "/playlist/{provider}/{id}", "guest", 404),
    ("/discover/new-releases", "/discover/new-releases", "guest", 200),
    ("/discover/trending/tracks", "/discover/trending/tracks", "guest", 200),
    ("/downloads", "/downloads", "user", 200),
    ("/downloads", "/downloads", "admin", 200),
    ("/downloads", "/downloads", "guest", 401),
    ("/favorites", "/favorites", "user", 200),
    ("/favorites", "/favorites", "guest", 401),
    ("/favorites/elsewhere", "/favorites/elsewhere", "user", 200),
    ("/favorites/elsewhere", "/favorites/elsewhere", "guest", 401),
    ("/library/sync/status", "/library/sync/status", "user", 200),
    ("/library/sync/status", "/library/sync/status", "admin", 200),
    ("/library/sync/status", "/library/sync/status", "guest", 401),
    ("/library/download-sync/status", "/library/download-sync/status", "user", 200),
    ("/library/download-sync/status", "/library/download-sync/status", "guest", 401),
    ("/library/jobs", "/library/jobs", "user", 200),
    ("/library/jobs", "/library/jobs", "admin", 200),
    ("/library/jobs", "/library/jobs", "guest", 401),
    ("/providers", "/providers", "user", 200),
    ("/providers", "/providers", "admin", 200),
    ("/providers", "/providers", "guest", 401),
    ("/providers/public", "/providers/public", "guest", 200),
    ("/settings/schema", "/settings/schema", "admin", 200),
    ("/settings/schema", "/settings/schema", "user", 200),
    ("/config/download-states", "/config/download-states", "guest", 200),
    ("/auth/account/status", "/auth/account/status", "guest", 200),
    ("/auth/account/status", "/auth/account/status", "user", 200),
    ("/auth/account/status", "/auth/account/status", "admin", 200),
    ("/auth/account/me", "/auth/account/me", "user", 200),
    ("/auth/account/me", "/auth/account/me", "guest", 401),
    ("/library/duplicates", "/library/duplicates", "admin", 200),
    ("/library/duplicates", "/library/duplicates", "user", 403),
    ("/system/routines", "/system/routines", "admin", 200),
    ("/system/routines", "/system/routines", "user", 403),
    ("/system/routines", "/system/routines", "guest", 401),
]


@pytest.mark.parametrize("url, path, role, status", _READS, ids=[f"{r[2]}:{r[0]}->{r[3]}" for r in _READS])
def test_the_response_matches_the_spec(api, url, path, role, status):
    resp = api.get(url, role)

    assert resp.status_code == status, resp.text
    assert_conforms(resp, "GET", path)


def test_album_editions_are_the_release_group_of_the_album(api):
    items = api.get("/album/catalog/a1/editions").json()["items"]
    assert [(e["provider_id"], e["year"]) for e in items] == [("a1", 2001), ("a1d", 2003)]


def test_the_scenarios_exercise_the_optional_fields(api):
    """Il seed copre davvero i campi di cui lo spec dichiara il tipo: se un
    campo sparisse dalla risposta, la validazione non lo vedrebbe più."""
    providers = api.get("/providers", "admin").json()
    download = providers["download"][0]
    for field in ("accounts", "account_display_fields", "borrowed_oauth_client_from", "borrowed_oauth_client_ok",
                  "disclaimer", "library_available", "library_package", "library_url", "setup_command",
                  "setup_instructions", "usable", "account_model", "config_valid", "also_registered_in"):
        assert field in download, field
    assert providers["identification"]

    searched = api.get("/search?q=song&type=playlist", "user").json()
    assert {p["tracks_unavailable"] is None for p in searched["items"]} == {True, False}

    status = api.get("/library/sync/status", "admin").json()
    assert "error_detail" in status
    assert "error_detail" not in api.get("/library/sync/status", "user").json()

    groups = api.get("/library/duplicates", "admin").json()["groups"]
    assert groups and len(groups[0]["members"]) == 2

    routines = api.get("/system/routines", "admin").json()["routines"]
    assert any(r["last_run"] for r in routines) and any(r["last_run"] is None for r in routines)

    favorites = api.get("/favorites", "user").json()["items"]
    assert favorites and all({"favorited_at", "favorite", "in_library", "playback", "library_id"} <= set(item) for item in favorites)
    assert favorites[0]["favorite"] and favorites[0]["playback"] == {"type": "full", "source": "library"}
    assert favorites[0]["stream_url"].startswith("/api/stream/")

    elsewhere = api.get("/favorites/elsewhere", "user").json()["items"]
    assert [item["title"] for item in elsewhere] == ["Elsewhere"]
    assert elsewhere[0]["provider_saved_at"]["catalog"]["added_at"] and not elsewhere[0]["in_library"]

    album = api.get("/album/catalog/a1", "user").json()
    assert {"favorite", "addable"} <= set(album["favorites"])

    downloads = api.get("/downloads", "admin").json()["items"]
    assert all(item["requested_by"] in ("user", "admin") for item in downloads)
    assert json.dumps(downloads).count('"error_detail"') > 0
