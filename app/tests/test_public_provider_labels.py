"""
``GET /providers/public``: un guest (autenticazione attiva, nessun token)
ottiene solo id ed etichette dei provider e l'ordine di priorità dei
metadati configurato. Nessuna credenziale, configurazione, stato o account.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from db.engine import get_session
from routes import deps
from routes.config import router as config_router
from services.config_manager import ConfigManager

from tests.database import new_engine
from tests.conftest import FakeDownloadProvider, FakeMetadataProvider, FakeRegistry, FakeSyncProvider


@pytest.fixture
def client():
    engine = new_engine()
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True, "metadata_provider_priority": ["metab", "unknown", "metaa"]})
    registry = FakeRegistry(
        metadata=[FakeMetadataProvider("metaa"), FakeMetadataProvider("metab")],
        downloads=[FakeDownloadProvider("dlone")],
        syncs=[FakeSyncProvider("syncone")],
    )
    app = FastAPI()
    app.include_router(config_router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    with TestClient(app) as c:
        yield c


def test_a_guest_gets_only_ids_labels_and_the_priority_order(client):
    resp = client.get("/providers/public")

    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"providers", "metadata_priority", "metadata", "metadata_capabilities"}
    assert all(set(p) == {"id", "label"} for p in data["providers"])
    assert {p["id"]: p["label"] for p in data["providers"]} == {
        "metaa": "Metaa", "metab": "Metab", "dlone": "Dlone", "syncone": "Syncone",
    }


def test_the_order_follows_the_configured_metadata_priority_without_unknown_names(client):
    assert client.get("/providers/public").json()["metadata_priority"] == ["metab", "metaa"]


def test_the_full_provider_list_still_needs_a_signed_in_user(client):
    assert client.get("/providers").status_code == 401


def test_every_metadata_provider_is_listed_in_priority_order(client):
    assert client.get("/providers/public").json()["metadata"] == ["metab", "metaa"]


def test_a_signed_in_user_gets_their_own_priority(client):
    from core.user_auth import create_access_token
    from db.models import User
    from db.settings_store import SettingsRepository

    engine = client.app.dependency_overrides[get_session]().__next__().get_bind()
    with Session(engine) as session:
        user = User(username="alice", password_hash="x", role="user")
        session.add(user)
        session.commit()
        SettingsRepository(session).set_user("metadata_provider_priority", user.id, ["metaa", "metab"])
        session.commit()
        token = create_access_token(user.id, user.role)

    data = client.get("/providers/public", headers={"Authorization": f"Bearer {token}"}).json()

    assert data["metadata_priority"] == ["metaa", "metab"]
    assert data["metadata"] == ["metaa", "metab"]
    # A guest still gets the system order.
    assert client.get("/providers/public").json()["metadata_priority"] == ["metab", "metaa"]


def test_the_capabilities_each_metadata_provider_declares_are_public(client):
    from musizerr_sdk.capabilities import CAPABILITY_DISCOGRAPHY

    registry = client.app.dependency_overrides[deps.get_registry]()
    registry.get_metadata_provider("metab").supports_discography = lambda: True

    capabilities = client.get("/providers/public").json()["metadata_capabilities"]

    assert capabilities == {"metaa": [], "metab": [CAPABILITY_DISCOGRAPHY]}
