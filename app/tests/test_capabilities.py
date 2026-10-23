"""
``GET /auth/account/status`` espone, per chi sta guardando, la modalità e le
capability calcolate dal server (``services/capabilities.py``). Le quattro
modalità: guest (auth attiva, nessun token), user, admin, open (auth
disattivata). Il test di coerenza chiama, in ogni modalità, la rotta che
ciascuna capability governa e verifica che flag ed esito HTTP concordino:
è il test che tiene onesto il contratto verso la UI.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from core import user_auth
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import Download, PendingLike, TrackRecord, User
from routes import deps
from routes.content.track import router as track_router
from routes.config import router as config_router
from routes.library import router as library_router
from routes.playlists import router as playlists_router
from routes.search import router as search_router
from routes.stream import router as stream_router
from routes.system.routines import router as routines_router
from routes.auth import router as auth_router
from routes.users_auth import router as account_router
from services import downloads

from tests.conftest import FakeAuthManager, FakeRegistry
from tests.database import seed_system_user
from tests.test_download_permissions import _Catalog

MODES = ("guest", "user", "admin", "open")

EXPECTED_CAPABILITIES = {
    "guest": dict(search=False, download=False, download_plain=False, favorite=False, stream_full=False,
                  library=False, playlists=False, settings_personal=False, linked_accounts=False, admin=False),
    "user": dict(search=True, download=True, download_plain=False, favorite=True, stream_full=True,
                 library=True, playlists=True, settings_personal=True, linked_accounts=True, admin=False),
    "admin": dict(search=True, download=True, download_plain=True, favorite=True, stream_full=True,
                  library=True, playlists=True, settings_personal=True, linked_accounts=True, admin=True),
    "open": dict(search=True, download=True, download_plain=True, favorite=True, stream_full=True,
                 library=True, playlists=True, settings_personal=True, linked_accounts=True, admin=True),
}


def test_open_mode_has_every_capability_of_an_admin():
    assert EXPECTED_CAPABILITIES["open"] == EXPECTED_CAPABILITIES["admin"]


def _user(engine, role: str, *, active: bool = True) -> User:
    with Session(engine) as session:
        user = User(username=f"{role}-{active}", password_hash="x", role=role, is_active=active)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app_for(engine, monkeypatch):
    from services.config_manager import ConfigManager

    queued: list = []
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: queued.append(kw))

    def build(auth_enabled: bool) -> TestClient:
        config = ConfigManager(engine=engine)
        config.update({"auth_enabled": auth_enabled})
        monkeypatch.setattr(downloads, "get_config", lambda: config)
        app = FastAPI()
        app.include_router(account_router, prefix="/auth/account")
        app.include_router(auth_router, prefix="/auth")
        for router in (search_router, track_router, stream_router, library_router, playlists_router, config_router,
                       routines_router):
            app.include_router(router)

        def _session():
            with Session(engine) as s:
                yield s

        registry = FakeRegistry(metadata=[_Catalog()])
        app.dependency_overrides[get_session] = _session
        app.dependency_overrides[deps.get_config] = lambda: config
        app.dependency_overrides[deps.get_registry] = lambda: registry
        app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
        return TestClient(app)

    return build


def _client_and_headers(mode, engine, app_for):
    client = app_for(auth_enabled=mode != "open")
    if mode == "open":
        seed_system_user(engine)
    if mode in ("user", "admin"):
        user = _user(engine, mode)
        return client, _bearer(create_access_token(user.id, user.role))
    return client, {}


@pytest.mark.parametrize("mode", MODES)
def test_status_reports_the_exact_mode_and_capabilities(mode, engine, app_for):
    client, headers = _client_and_headers(mode, engine, app_for)

    data = client.get("/auth/account/status", headers=headers).json()

    assert data["mode"] == mode
    assert data["capabilities"] == EXPECTED_CAPABILITIES[mode]


def test_an_inactive_user_is_a_guest(engine, app_for):
    client = app_for(auth_enabled=True)
    user = _user(engine, "user", active=False)

    data = client.get("/auth/account/status", headers=_bearer(create_access_token(user.id, user.role))).json()

    assert data["mode"] == "guest"
    assert data["capabilities"] == EXPECTED_CAPABILITIES["guest"]


def test_an_expired_token_is_a_guest(engine, app_for):
    client = app_for(auth_enabled=True)
    user = _user(engine, "user")
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    expired = jwt.encode(
        {"sub": user.id, "role": user.role, "type": "access", "iat": past, "exp": past + timedelta(minutes=1)},
        user_auth._get_secret(), algorithm=user_auth._ALGORITHM,
    )

    data = client.get("/auth/account/status", headers=_bearer(expired)).json()

    assert data["mode"] == "guest"


# ---------------------------------------------------------------------------
# Coerenza: ogni capability contro la rotta che governa
# ---------------------------------------------------------------------------

GOVERNED = {
    "search": ("GET", "/search?q=x", None),
    "download": ("POST", "/download/track", {"provider": "catalog", "id": "x"}),
    # Un download semplice: nessun preferito all'arrivo (vedi _plain_download).
    "download_plain": ("POST", "/download/track", {"provider": "catalog", "id": "x", "add_to_favorites": False}),
    "favorite": ("PUT", "/favorites/{library_track}", None),
    "stream_full": ("GET", "/stream/no-such-track", None),
    "library": ("GET", "/favorites", None),
    "playlists": ("GET", "/playlists", None),
    "settings_personal": ("PATCH", "/settings", {"language": "it"}),
    "linked_accounts": ("GET", "/auth/linked-accounts", None),
    "admin": ("GET", "/system/routines", None),
}


def _allowed(status_code: int) -> bool:
    # 404 = accesso consentito, risorsa assente (es. nessun file scaricato).
    return status_code < 400 or status_code == 404


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("capability", sorted(GOVERNED))
def test_each_capability_matches_the_real_http_outcome(mode, capability, engine, app_for):
    client, headers = _client_and_headers(mode, engine, app_for)
    flag = client.get("/auth/account/status", headers=headers).json()["capabilities"][capability]
    method, path, body = GOVERNED[capability]
    path = path.replace("{library_track}", _library_track(engine))

    resp = client.request(method, path, json=body, headers=headers)

    allowed = _allowed(resp.status_code)
    if capability == "download_plain":
        # Il download parte anche per un utente, ma col preferito all'arrivo:
        # "semplice" solo se non resta nessun intento.
        allowed = allowed and _intents(engine) == 0
    assert allowed is flag, (
        f"{mode}: capability {capability}={flag} ma {method} {path} -> {resp.status_code} {resp.text[:200]}"
    )


def _library_track(engine) -> str:
    """Una traccia in libreria (un download arrivato)."""
    with Session(engine) as session:
        record = TrackRecord(title="In", artist="Library", album="A")
        session.add(record)
        session.commit()
        session.add(Download(track_id=record.internal_id, status="downloaded"))
        session.commit()
        return record.internal_id


def _intents(engine) -> int:
    with Session(engine) as session:
        return len(session.exec(select(PendingLike)).all())
