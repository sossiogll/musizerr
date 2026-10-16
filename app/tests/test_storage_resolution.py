"""services.storage.resolve_storage_provider: lo storage è quello del setting
``storage_provider``, che NON ha un default (il catalogo non nomina provider):
senza un valore, o con un provider non più registrato, non c'è nessuno
storage — mai un nome scritto a mano che ricada su qualcun altro.
"""

from __future__ import annotations

import pytest

from core import settings_catalog as sc
from services.storage import resolve_storage_provider

from tests.database import new_engine
from tests.conftest import FakeRegistry, FakeStorageProvider


def test_the_storage_provider_setting_has_no_default_and_is_optional():
    setting = sc.get_def("storage_provider")

    assert setting.default is None
    assert not setting.required


def test_resolve_storage_provider_uses_the_configured_name_when_it_resolves():
    configured = FakeStorageProvider("shelf")
    registry = FakeRegistry(storage=[configured])

    storage, name = resolve_storage_provider(registry, "shelf")

    assert storage.name == "shelf"
    assert name == "shelf"


def test_resolve_storage_provider_finds_nothing_when_unset_even_if_one_is_registered():
    registry = FakeRegistry(storage=[FakeStorageProvider("shelf")])

    storage, name = resolve_storage_provider(registry, None)

    assert storage is None
    assert name is None


def test_resolve_storage_provider_does_not_fall_back_when_configured_provider_is_gone():
    registry = FakeRegistry(storage=[FakeStorageProvider("shelf")])

    storage, name = resolve_storage_provider(registry, "uninstalled-provider")

    assert storage is None
    assert name == "uninstalled-provider"


def test_resolve_storage_provider_returns_none_when_nothing_resolves():
    registry = FakeRegistry(storage=[])

    storage, name = resolve_storage_provider(registry, "missing")

    assert storage is None


# ---------------------------------------------------------------------------
# Rotte di stream senza nessuno storage provider
# ---------------------------------------------------------------------------


@pytest.fixture(params=["gone", None], ids=["configured-but-not-registered", "not-set"])
def no_storage_client(request):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from db.engine import get_session
    from db.models import Download, TrackIdentifierRecord, TrackRecord
    from routes import deps
    from routes.stream import router as stream_router
    from services.config_manager import ConfigManager
    from tests.conftest import FakeAuthManager

    engine = new_engine()
    with Session(engine) as session:
        record = TrackRecord(title="T", artist="A", album="B")
        session.add(record)
        session.commit()
        session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type="pa_id", value="p1"))
        session.add(Download(track_id=record.internal_id, status="downloaded", file_path="A/B/T.mp3"))
        session.commit()
        track_id = record.internal_id
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False})
    if request.param is not None:
        config.update({"storage_provider": request.param})
    app = FastAPI()
    app.include_router(stream_router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: FakeRegistry()
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    return TestClient(app), track_id


def test_streaming_without_any_storage_provider_is_a_503(no_storage_client):
    client, track_id = no_storage_client

    assert client.get(f"/stream/{track_id}").status_code == 503


def test_playback_without_any_storage_provider_is_not_full(no_storage_client):
    client, _track_id = no_storage_client

    resp = client.get("/playback/pa/p1")

    assert resp.status_code == 200 and resp.json()["type"] != "full"
