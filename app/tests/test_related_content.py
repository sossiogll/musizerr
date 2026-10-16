"""
``GET /track/{provider}/{id}/related`` e ``/appears-on``: generiche su
tutti i provider di metadati in ordine di priorità, deduplicate col
matching di core, con lo stato di download sulle tracce; un provider che
fallisce viene saltato.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.interfaces import MetadataProvider
from musizerr_sdk.context import StaticProviderContext
from db.engine import get_session
from db.models import Download, TrackIdentifierRecord, TrackRecord
from routes import deps
from routes.content.track import router as track_router
from services.config_manager import ConfigManager

from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRegistry, make_track


def _t(provider, pid, *, title="Larsen", album="Prisoner 709", year=2017, isrc=None, mbid=None):
    return make_track(title=title, artist="Caparezza", album=album, year=year,
                      provider=provider, provider_id=pid, isrc=isrc, mbid=mbid)


class _Fake(MetadataProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name, *, track=None, related=(), by_isrc=None, fail=False):
        super().__init__(StaticProviderContext())
        self.name, self.label = name, name
        self._track, self._related, self._by_isrc, self._fail = track, list(related), by_isrc, fail

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_track(self, provider_id, session):
        return self._track

    def find_related_tracks(self, track, session, limit=20):
        if self._fail:
            raise RuntimeError("503")
        return self._related[:limit]

    def supports_isrc_lookup(self):
        return True

    def find_by_isrc(self, isrc, session):
        if self._fail:
            raise RuntimeError("503")
        return self._by_isrc


def _client(providers, priority, engine=None):
    engine = engine or _engine()
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False, "metadata_provider_priority": priority})
    app = FastAPI()
    app.include_router(track_router)

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


SOURCE = _t("pa", "src", isrc="IT123")


def test_related_follows_priority_and_dedupes_with_core_matching():
    pa = _Fake("pa", track=SOURCE, related=[_t("pa", "pa-live", title="Larsen (Live)", album="Live 2018", year=2018)])
    pb = _Fake("pb", related=[
        _t("pb", "pb-remaster", title="Larsen - Remastered", album="Prisoner 709", year=2017),
        _t("pb", "pb-other", title="Ti fa stare bene", year=2017),
    ])
    pc = _Fake("pc", related=[_t("pc", "pc-dupe", title="Larsen (Live)", album="Live 2018", year=2018)])

    items = _client([pa, pb, pc], ["pb", "pa", "pc"]).get("/track/pa/src/related").json()["items"]

    ids = [i["provider_id"] for i in items]
    assert ids[0] == "pb-remaster"  # il provider con priorità più alta viene prima
    assert "pc-dupe" not in ids  # stessa traccia del live di pa: deduplicata
    assert "src" not in ids
    assert all(i["kind"] == "track" for i in items)


def test_related_respects_the_limit():
    pa = _Fake("pa", track=SOURCE, related=[_t("pa", f"r{i}", title=f"Song {i}") for i in range(10)])

    assert len(_client([pa], ["pa"]).get("/track/pa/src/related?limit=3").json()["items"]) == 3


def test_related_tracks_carry_their_download_status():
    engine = _engine()
    related = _t("pa", "rel-1", title="Confusianesimo")
    with Session(engine) as session:
        record = TrackRecord(title="Confusianesimo", artist="Caparezza", album="Prisoner 709")
        session.add(record)
        session.commit()
        session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type="pa_id", value="rel-1"))
        session.add(Download(track_id=record.internal_id, status="downloaded"))
        session.commit()

    items = _client([_Fake("pa", track=SOURCE, related=[related])], ["pa"], engine).get("/track/pa/src/related").json()["items"]

    assert items[0]["download_status"] == "downloaded"


def test_related_is_empty_when_nobody_has_anything_and_survives_a_failing_provider():
    providers = [_Fake("pa", track=SOURCE), _Fake("pb", fail=True)]

    resp = _client(providers, ["pb", "pa"]).get("/track/pa/src/related")

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    # Il provider che ha fallito è nominato: la lista può essere incompleta.
    assert [e["provider"] for e in body["provider_errors"]] == ["pb"]
    assert body["provider_errors"][0]["error_code"].startswith("metadata_provider_")


def test_appears_on_lists_releases_of_the_same_recording_once_each():
    pa = _Fake("pa", track=SOURCE, related=[
        _t("pa", "pa-2", album="Prisoner 709 (Deluxe)", year=2018, isrc="IT123"),
        _t("pa", "pa-3", title="Larsen", album="Another Album", year=2019),  # nessun ISRC in comune
    ])
    pb = _Fake("pb", by_isrc=_t("pb", "pb-1", album="Prisoner 709", year=2017, isrc="IT123"))

    items = _client([pa, pb], ["pb", "pa"]).get("/track/pa/src/appears-on").json()["items"]

    titles = [(i["title"], i["year"]) for i in items]
    assert titles == [("Prisoner 709", 2017), ("Prisoner 709 (Deluxe)", 2018)]
    assert items[0]["provider"] == "pb"  # stessa uscita da due provider: vince la priorità


def test_appears_on_without_identifiers_elsewhere_is_just_the_own_release():
    pa = _Fake("pa", track=_t("pa", "src"))

    items = _client([pa, _Fake("pb", fail=True)], ["pa", "pb"]).get("/track/pa/src/appears-on").json()["items"]

    assert [(i["title"], i["provider"]) for i in items] == [("Prisoner 709", "pa")]


def test_an_unknown_track_is_a_404():
    assert _client([_Fake("pa", track=None)], ["pa"]).get("/track/pa/nope/related").status_code == 404
