"""
``GET /artist/{provider}/{id}/top-tracks``: le tracce più popolari prima dal
provider richiesto, poi, finché ne mancano, dagli altri provider in ordine
di priorità (l'artista si ritrova con l'arricchimento incrociato),
deduplicate con ``match_track`` e con lo stato di download. Un provider che
fallisce non rompe la pagina.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizeer_sdk.identifiers import PROVIDER_ID, Identifier
from musizeer_sdk.interfaces import MetadataProvider
from musizeer_sdk.models import Artist
from musizeer_sdk.context import StaticProviderContext
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import Download, TrackIdentifierRecord, TrackRecord, User
from routes import deps
from routes.content.artist import router as artist_router
from services.config_manager import ConfigManager

from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeMetadataProvider, FakeRegistry, make_track


def _t(provider, pid, title, album="Prisoner 709"):
    return make_track(title=title, artist="Caparezza", album=album, year=2017, provider=provider, provider_id=pid)


class _Artists(MetadataProvider):
    """Provider finto con un artista e le sue top tracks; ``fail`` fa sollevare
    la lettura delle top tracks, ``fail_artist`` quella dell'artista."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name, top=(), *, fail=False, fail_artist=False):
        super().__init__(StaticProviderContext())
        self.name, self.label = name, name
        self._top, self._fail, self._fail_artist = list(top), fail, fail_artist
        self.requested_ids: list[str] = []

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def _artist(self):
        artist = Artist(name="Caparezza", provider=self.name, provider_id=f"{self.name}-art")
        artist.add_id(Identifier(type=PROVIDER_ID, value=f"{self.name}-art", provider=self.name))
        return artist

    def get_artist(self, provider_id, session):
        if self._fail_artist:
            raise RuntimeError("[Errno -3] Temporary failure in name resolution")
        return self._artist()

    def search_artists(self, query, session, limit=20):
        return [self._artist()]

    def get_artist_top_tracks(self, provider_id, session, limit=10):
        self.requested_ids.append(provider_id)
        if self._fail:
            raise RuntimeError("[Errno -3] Temporary failure in name resolution")
        return self._top[:limit]


def _client(providers, priority, *, engine=None, auth_enabled=False):
    engine = engine or _engine()
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": auth_enabled, "metadata_provider_priority": priority})
    app = FastAPI()
    app.include_router(artist_router)

    def _session():
        with Session(engine) as s:
            yield s

    registry = FakeRegistry(metadata=providers)
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    return TestClient(app)


def _engine():
    engine = new_engine()
    return engine


def _ids(resp):
    return [item["provider_id"] for item in resp.json()["items"]]


def test_the_default_interface_method_returns_nothing():
    assert FakeMetadataProvider("x").get_artist_top_tracks("a", AuthSession.anonymous(), 10) == []


def test_the_provider_s_order_is_kept_and_limit_is_respected():
    top = [_t("pa", f"t{i}", f"Song {i}") for i in range(8)]

    resp = _client([_Artists("pa", top)], ["pa"]).get("/artist/pa/pa-art/top-tracks?limit=5")

    assert resp.status_code == 200
    assert _ids(resp) == ["t0", "t1", "t2", "t3", "t4"]
    assert all(item["kind"] == "track" for item in resp.json()["items"])


def test_an_empty_requested_provider_falls_back_to_the_next_by_priority():
    pa = _Artists("pa", [])
    pb = _Artists("pb", [_t("pb", "b1", "Larsen")])
    pc = _Artists("pc", [_t("pc", "c1", "Confusianesimo")])

    resp = _client([pa, pb, pc], ["pa", "pc", "pb"]).get("/artist/pa/pa-art/top-tracks?limit=1")

    assert _ids(resp) == ["c1"]  # pc viene prima di pb nella priorità
    assert pc.requested_ids == ["pc-art"]  # l'artista ritrovato tramite arricchimento


def test_dedup_across_providers_merges_variants_but_not_near_misses():
    pa = _Artists("pa", [_t("pa", "a1", "Larsen"), _t("pa", "a2", "Prisoner 709")])
    pb = _Artists("pb", [
        _t("pb", "b1", "Larsen (feat. Nobody)"),    # stessa traccia: si unisce
        _t("pb", "b2", "Prisoner 709 - Remastered"),  # stessa traccia: si unisce
        _t("pb", "b3", "Prisoner 7"),                # titolo solo simile: resta
    ])

    resp = _client([pa, pb], ["pa", "pb"]).get("/artist/pa/pa-art/top-tracks?limit=10")

    assert _ids(resp) == ["a1", "a2", "b3"]


def test_download_status_is_annotated():
    engine = _engine()
    with Session(engine) as session:
        record = TrackRecord(title="Larsen", artist="Caparezza", album="Prisoner 709")
        session.add(record)
        session.commit()
        session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type="pa_id", value="a1"))
        session.add(Download(track_id=record.internal_id, status="downloaded"))
        session.commit()
    pa = _Artists("pa", [_t("pa", "a1", "Larsen"), _t("pa", "a2", "Larsen Live")])

    items = _client([pa], ["pa"], engine=engine).get("/artist/pa/pa-art/top-tracks").json()["items"]

    assert [i["download_status"] for i in items] == ["downloaded", None]


def test_a_failing_first_provider_still_falls_back():
    pa = _Artists("pa", fail=True)
    pb = _Artists("pb", [_t("pb", "b1", "Larsen")])

    resp = _client([pa, pb], ["pa", "pb"]).get("/artist/pa/pa-art/top-tracks")

    assert resp.status_code == 200
    assert _ids(resp) == ["b1"]
    assert resp.json()["error_code"] is None


def test_when_every_provider_fails_the_list_is_empty_with_a_reason():
    resp = _client([_Artists("pa", fail=True), _Artists("pb", fail=True)], ["pa", "pb"]).get("/artist/pa/pa-art/top-tracks")

    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["error_code"] == "metadata_provider_unreachable"
    assert "Errno" not in resp.text


def test_a_failing_artist_lookup_is_a_reason_not_a_500():
    resp = _client([_Artists("pa", fail_artist=True)], ["pa"]).get("/artist/pa/pa-art/top-tracks")

    assert resp.status_code == 200
    assert resp.json()["items"] == [] and resp.json()["error_code"]


def test_nothing_anywhere_is_an_empty_list_without_a_reason():
    resp = _client([_Artists("pa"), _Artists("pb")], ["pa", "pb"]).get("/artist/pa/pa-art/top-tracks")

    assert resp.json() == {"items": [], "error_code": None, "error_message": None, "provider_errors": []}


@pytest.mark.parametrize("mode", ["guest", "user", "admin", "open"])
def test_public_like_the_other_artist_detail_routes(mode):
    engine = _engine()
    headers = {}
    if mode in ("user", "admin"):
        with Session(engine) as session:
            user = User(username=mode, password_hash="x", role=mode)
            session.add(user)
            session.commit()
            session.refresh(user)
            headers = {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}
    client = _client([_Artists("pa", [_t("pa", "a1", "Larsen")])], ["pa"], engine=engine, auth_enabled=mode != "open")

    top = client.get("/artist/pa/pa-art/top-tracks", headers=headers)
    detail = client.get("/artist/pa/pa-art", headers=headers)

    assert top.status_code == detail.status_code == 200


class _ListWithoutIsrc(_Artists):
    """A provider whose list endpoint omits the ISRC that its track detail has
    (real case: the top tracks falling back to a second provider when the
    first answered 403 — owned tracks were offered for download)."""

    def __init__(self, name, top, isrc_by_id):
        super().__init__(name, top)
        self._isrc = isrc_by_id
        self.details: list[str] = []

    def get_track(self, provider_id, session):
        self.details.append(provider_id)
        track = next(t for t in self._top if t.provider_id == provider_id)
        full = make_track(title=track.title, artist=track.artist, album=track.album, year=2017,
                          provider=self.name, provider_id=provider_id, isrc=self._isrc.get(provider_id))
        return full


def test_a_list_row_without_isrc_is_completed_so_an_owned_track_shows_as_in_the_library():
    engine = _engine()
    with Session(engine) as session:
        # In the library through another provider's id and its ISRC only.
        record = TrackRecord(title="Larsen", artist="Caparezza", album="Prisoner 709")
        session.add(record)
        session.commit()
        session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type="pa_id", value="a1"))
        session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type="isrc", value="IT0001"))
        session.add(Download(track_id=record.internal_id, status="downloaded"))
        known = TrackRecord(title="Confusianesimo", artist="Caparezza", album="Prisoner 709")
        session.add(known)
        session.commit()
        session.add(TrackIdentifierRecord(track_id=known.internal_id, id_type="pb_id", value="b2"))
        session.commit()
    pb = _ListWithoutIsrc("pb", [_t("pb", "b1", "Larsen"), _t("pb", "b2", "Confusianesimo"), _t("pb", "b3", "Prosopagnosia")],
                          {"b1": "IT0001", "b3": "IT0003"})

    items = _client([pb], ["pb"], engine=engine).get("/artist/pb/pb-art/top-tracks").json()["items"]

    assert [i["download_status"] for i in items] == ["downloaded", None, None]
    assert items[0]["in_library"] is True
    # Only the rows the database cannot place are completed: b2 is known by its id.
    assert pb.details == ["b1", "b3"]
