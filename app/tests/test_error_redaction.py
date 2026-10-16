"""
Errori grezzi dell'infrastruttura (es. "[Errno -3] Temporary failure in name
resolution") mai nelle risposte a chi non è admin: un utente riceve
``error_code`` e un messaggio leggibile; un admin, e la modalità open (dove
tutti sono l'admin incorporato), ricevono anche ``error_detail``.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import SYSTEM_USER_ID, LibraryJobRun, User
from db.routine_run_store import STATUS_ERROR, claim_run, finish_run, queue_run
from routes import deps
from routes.auth import router as auth_router
from routes.library import router as library_router
from routes.system.routines import router as routines_router
from services.config_manager import ConfigManager
from services.errors import classify, public_error

from tests.conftest import FakeMetadataProvider, FakeRegistry
from tests.database import seed_system_user

RAW = "[Errno -3] Temporary failure in name resolution"


class _BrokenProvider(FakeMetadataProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def test_connection(self, session):
        return RAW


def _user(engine, role: str) -> User:
    with Session(engine) as session:
        user = User(username=role, password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _failed_sync(engine, user_id: str) -> None:
    with Session(engine) as session:
        body = public_error(RAW, "metadata_provider", show_detail=True)
        session.add(LibraryJobRun(
            user_id=user_id, kind="library_sync", status="failed", error_code=body["error_code"],
            error_message=body["error_message"], error_detail=body["error_detail"],
            started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc),
        ))
        session.commit()


def _failed_routine(engine) -> None:
    with Session(engine) as session:
        run = claim_run(session, queue_run(session, "sweep_delegations").id)
        finish_run(session, run.id, status=STATUS_ERROR, detail=RAW)


@pytest.fixture(params=["user", "admin", "open"])
def viewer(request, engine):
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": request.param != "open"})
    app = FastAPI()
    app.include_router(library_router)
    app.include_router(routines_router)
    app.include_router(auth_router, prefix="/auth")

    def _session():
        with Session(engine) as s:
            yield s

    registry = FakeRegistry(metadata=[_BrokenProvider("fakemeta")])
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry

    if request.param == "open":
        seed_system_user(engine)
        user_id, headers = SYSTEM_USER_ID, {}
    else:
        user = _user(engine, request.param)
        user_id, headers = user.id, {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}
    with TestClient(app) as client:
        yield {"mode": request.param, "client": client, "headers": headers, "user_id": user_id}


def test_library_sync_status(viewer, engine):
    _failed_sync(engine, viewer["user_id"])

    resp = viewer["client"].get("/library/sync/status", headers=viewer["headers"])

    assert resp.status_code == 200
    data = resp.json()
    assert data["error_code"] == "metadata_provider_unreachable"
    assert data["error_message"]
    if viewer["mode"] == "user":
        assert RAW not in resp.text
    else:
        assert data["error_detail"] == RAW


def test_routine_result(viewer, engine):
    _failed_routine(engine)

    resp = viewer["client"].get("/system/routines", headers=viewer["headers"])

    if viewer["mode"] == "user":
        assert resp.status_code == 403
        assert RAW not in resp.text
        return
    run = next(r for r in resp.json()["routines"] if r["key"] == "sweep_delegations")["last_run"]
    assert run["error_code"] == "routine_unreachable"
    assert run["error_detail"] == RAW


def test_provider_connection_test(viewer):
    resp = viewer["client"].post("/auth/fakemeta/test?scope=account", headers=viewer["headers"])

    assert resp.status_code == 200
    account = resp.json()["account"]
    assert account["ok"] is False
    assert account["error_code"] == "provider_unreachable"
    if viewer["mode"] == "user":
        assert RAW not in resp.text
    else:
        assert account["error_detail"] == RAW


@pytest.mark.parametrize("raw, kind", [
    (RAW, "unreachable"),
    ("Connection refused", "unreachable"),
    ("Read timed out", "timeout"),
    ("401 Unauthorized", "auth_failed"),
    ("something odd happened", "failed"),
    ("HTTP 403: Forbidden", "auth_failed"),
    ("track 4012 has no source", "failed"),
    ("album id 94031 missing", "failed"),
])
def test_classification(raw, kind):
    assert classify(raw) == kind


def test_the_detail_is_only_added_on_request():
    assert "error_detail" not in public_error(RAW, "provider", show_detail=False)
    assert public_error(RAW, "provider", show_detail=True)["error_detail"] == RAW


# ---------------------------------------------------------------------------
# GET /downloads: l'errore di ogni tentativo
# ---------------------------------------------------------------------------


def _history_client(engine):
    from routes.history import router as history_router
    from tests.conftest import FakeAuthManager

    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})
    app = FastAPI()
    app.include_router(history_router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: FakeRegistry()
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    return TestClient(app)


def _failed_download(engine, user_id: str, error: str) -> None:
    from db.models import Download, TrackRecord

    with Session(engine) as session:
        record = TrackRecord(title="T", artist="A", album="B")
        session.add(record)
        session.commit()
        session.add(Download(track_id=record.internal_id, status="failed", requested_by=user_id, error_message=error))
        session.commit()


@pytest.mark.parametrize("raw, code", [
    ("Storage upload failed: [Errno 13] Permission denied: '/srv/music/A/B/T.mp3'", "download_provider_unreachable"),
    ("No provider could download 'T' by 'A'", "download_provider_not_found"),
])
def test_a_user_sees_download_errors_without_the_raw_text(engine, raw, code):
    user = _user(engine, "user")
    _failed_download(engine, user.id, raw)

    resp = _history_client(engine).get("/downloads", headers=_auth(user))

    attempt = resp.json()["items"][0]["attempts"][0]
    assert attempt["error_code"] == code and attempt["error_message"]
    assert "error_detail" not in attempt
    assert raw not in resp.text and "/srv/music" not in resp.text


def test_an_admin_also_sees_the_raw_download_error(engine):
    admin = _user(engine, "admin")
    _failed_download(engine, admin.id, RAW)

    attempt = _history_client(engine).get("/downloads", headers=_auth(admin)).json()["items"][0]["attempts"][0]

    assert attempt["error_code"] == "download_provider_unreachable"
    assert attempt["error_detail"] == RAW


def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}
