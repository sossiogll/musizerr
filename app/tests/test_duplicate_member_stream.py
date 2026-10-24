"""
``GET /library/duplicates/members/{member_id}/stream``: an admin hears ONE
file of a duplicate group before choosing the one to keep. The file is found
by the member's own identity — its sync id on the sync provider, else its
real path on the storage (the download it is linked to, never the member's
virtual path) — and never through the track-level stream. Admin only, with
the streaming cookie or a bearer token.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from musizeer_sdk.interfaces import StorageStreamResult
from core.user_auth import STREAM_COOKIE_NAME, create_access_token, create_stream_token
from db.engine import get_session
from db.models import Download, DuplicateGroup, DuplicateMember, TrackRecord, User
from routes import deps
from routes import stream as stream_route
from routes.errors import install_error_handlers
from services import public_urls
from services.config_manager import ConfigManager
from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRegistry, FakeStorageProvider, FakeSyncProvider


class _Storage(FakeStorageProvider):
    def stream(self, relative_path, workspace, session):
        if relative_path not in self.files:
            raise FileNotFoundError(relative_path)
        return StorageStreamResult(chunks=iter([self.files[relative_path]]), media_type="audio/ogg")


class _FailingSync(FakeSyncProvider):
    def get_stream_url(self, sync_id, session):
        raise httpx.ConnectError("sync provider down")


def _upstream(request: httpx.Request) -> httpx.Response:
    """The sync provider's audio: says which file was asked for."""
    return httpx.Response(200, content=f"sync:{request.url.path.strip('/')}".encode(), headers={"content-type": "audio/mpeg"})


@pytest.fixture
def env(monkeypatch):
    engine = new_engine()
    with Session(engine) as session:
        record = TrackRecord(title="Song", artist="Artist", album="Album")
        session.add(record)
        session.flush()
        download = Download(track_id=record.internal_id, status="downloaded", file_path="real/musizeer.ogg",
                            completed_at=datetime(2026, 9, 1, tzinfo=timezone.utc), provider_used="dl")
        group = DuplicateGroup(recording_id="rec", status="needs_review", reason="external_or_unknown_origin_would_be_deleted")
        session.add_all([download, group])
        session.flush()
        # Same virtual path on screen: two different files.
        ours = DuplicateMember(group_id=group.id, sync_id="sync-ours", path="Artist/Album/Song.mp3", title="Song",
                               artist="Artist", origin="musizeer", download_id=download.id, quality_known=True,
                               suffix="ogg", bitrate=332_000)
        external = DuplicateMember(group_id=group.id, sync_id="sync-ext", path="Artist/Album/Song.mp3", title="Song",
                                   artist="Artist", origin="unknown", quality_known=True, suffix="mp3", bitrate=128_000)
        gone = DuplicateMember(group_id=group.id, sync_id="sync-gone", path="x.mp3", title="Song", artist="Artist",
                               deleted_at=datetime(2026, 9, 2, tzinfo=timezone.utc))
        admin = User(username="admin", password_hash="x", role="admin")
        user = User(username="user", password_hash="x", role="user")
        session.add_all([ours, external, gone, admin, user])
        session.commit()
        ids = {"ours": ours.id, "external": external.id, "gone": gone.id, "admin": admin.id, "user": user.id,
               "group": group.id}

    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True, "storage_provider": "shelf", "sync_provider": "fakesync"})
    storage = _Storage("shelf")
    storage.files["real/musizeer.ogg"] = b"stored-audio"
    providers = {"storage": storage, "sync": FakeSyncProvider()}
    monkeypatch.setattr(stream_route, "_upstream_client", lambda: httpx.Client(transport=httpx.MockTransport(_upstream)))

    app = FastAPI()
    app.include_router(stream_route.router)
    install_error_handlers(app)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: FakeRegistry(storage=[providers["storage"]], syncs=[providers["sync"]])
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    return TestClient(app), ids, providers, config, engine


def _url(member_id: str) -> str:
    return f"/library/duplicates/members/{member_id}/stream"


def _bearer(user_id: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id, role)}"}


# --- access ---------------------------------------------------------------

def test_anonymous_is_401(env):
    client, ids, *_ = env
    assert client.get(_url(ids["ours"])).status_code == 401


def test_a_user_is_403_with_a_bearer_token_and_with_the_stream_cookie(env):
    client, ids, *_ = env
    assert client.get(_url(ids["ours"]), headers=_bearer(ids["user"], "user")).status_code == 403
    client.cookies.set(STREAM_COOKIE_NAME, create_stream_token(ids["user"]))
    assert client.get(_url(ids["ours"])).status_code == 403


def test_an_admin_streams_with_the_stream_cookie(env):
    client, ids, *_ = env
    client.cookies.set(STREAM_COOKIE_NAME, create_stream_token(ids["admin"]))
    response = client.get(_url(ids["ours"]))
    assert response.status_code == 200 and response.content == b"sync:sync-ours"


# --- which file -------------------------------------------------------------

def test_each_member_plays_its_own_file_by_sync_id(env):
    client, ids, *_ = env
    headers = _bearer(ids["admin"], "admin")
    assert client.get(_url(ids["ours"]), headers=headers).content == b"sync:sync-ours"
    assert client.get(_url(ids["external"]), headers=headers).content == b"sync:sync-ext"


def test_without_a_sync_provider_the_real_storage_path_plays_never_the_virtual_one(env):
    client, ids, _providers, config, _ = env
    config.update({"sync_provider": None})
    response = client.get(_url(ids["ours"]), headers=_bearer(ids["admin"], "admin"))
    assert response.status_code == 200 and response.content == b"stored-audio"


def test_a_failing_sync_provider_falls_back_to_the_stored_file(env):
    client, ids, providers, *_ = env
    providers["sync"] = _FailingSync()
    response = client.get(_url(ids["ours"]), headers=_bearer(ids["admin"], "admin"))
    assert response.status_code == 200 and response.content == b"stored-audio"


def test_a_failing_sync_provider_without_a_stored_file_is_a_provider_failure_not_a_500(env):
    client, ids, providers, *_ = env
    providers["sync"] = _FailingSync()
    response = client.get(_url(ids["external"]), headers=_bearer(ids["admin"], "admin"))
    assert response.status_code == 503


# --- 404 ------------------------------------------------------------------

def test_an_unknown_member_is_404(env):
    client, ids, *_ = env
    assert client.get(_url("nope"), headers=_bearer(ids["admin"], "admin")).status_code == 404


def test_a_deleted_file_is_404(env):
    client, ids, *_ = env
    assert client.get(_url(ids["gone"]), headers=_bearer(ids["admin"], "admin")).status_code == 404


def test_a_member_no_source_can_play_is_404(env):
    client, ids, _providers, config, _ = env
    config.update({"sync_provider": None})
    assert client.get(_url(ids["external"]), headers=_bearer(ids["admin"], "admin")).status_code == 404


# --- what the duplicates list says about each member -------------------------

def test_the_list_gives_each_member_its_stream_real_path_and_origin_data(env):
    from routes.library_duplicates import get_duplicate_groups

    _client, ids, _providers, config, engine = env
    with Session(engine) as session:
        group = get_duplicate_groups(admin=None, session=session, config=config, status=None)["groups"][0]
    members = {m["id"]: m for m in group["members"]}

    ours, external, gone = members[ids["ours"]], members[ids["external"]], members[ids["gone"]]
    assert ours["stream_url"] == public_urls.duplicate_member_stream_url(ids["ours"])
    assert ours["storage_path"] == "real/musizeer.ogg" and ours["path"] == "Artist/Album/Song.mp3"
    assert ours["added_at"].startswith("2026-09-01") and ours["downloaded_via"] == "dl"
    assert external["storage_path"] is None and external["added_at"] is None
    assert gone["stream_url"] is None
    # The 332 kbps file is the best quality: a hint, never a keeper.
    assert ours["best_quality"] is True and external["best_quality"] is False
    assert not any(m["is_keeper"] for m in group["members"])
