"""
Test di ``routes/library_duplicates.py``: permessi reali (via TestClient,
stesso stile di ``test_routines_api_permissions.py``) + logica di
risoluzione chiamata direttamente (stesso stile di
``test_stream_permissions.py``) — risolvere cancella SOLO i membri
cancellabili, mai il keeper ne' un'origine esterna, e triggera il rescan.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from musizerr_sdk.auth import AuthSession
from services.config_manager import ConfigManager
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import Download, DuplicateGroup, DuplicateMember, User
from routes import deps
from routes.library_duplicates import (
    dismiss_duplicate_group,
    get_duplicate_groups,
    resolve_all_duplicate_groups,
    resolve_duplicate_group,
    ResolveBody,
)
from tests.database import new_engine
from tests.conftest import FakeRegistry, FakeStorageProvider, FakeSyncProvider
from tests.database import seed_parents


class _FakeAuthManager:
    def authenticate(self, provider):
        return AuthSession.anonymous()


def _make_group(session: Session, *, status: str = "proposed") -> tuple[DuplicateGroup, DuplicateMember, DuplicateMember]:
    """``keeper`` non ha mai un ``download_id`` (origine ignota, ma e' il
    keeper: non viene mai cancellato comunque). ``loser`` e' un vero
    ``Download`` musizerr — il suo ``file_path`` REALE ("real/loser.mp3") e'
    diverso dal path VIRTUALE del membro ("loser.mp3"), esattamente come nella
    realta' (vedi musizerr_sdk.models.LibraryItem): la risoluzione deve cancellare il
    path reale via download_id, mai il path virtuale del membro."""
    group = DuplicateGroup(recording_id="rec-1", status=status, reason="best_quality")
    session.add(group)
    seed_parents(session, tracks=["t-1"])
    session.flush()
    download = Download(track_id="t-1", status="downloaded", file_path="real/loser.mp3")
    session.add(download)
    session.flush()
    keeper = DuplicateMember(
        group_id=group.id, sync_id="s1", path="keeper.flac", title="Song", artist="Artist",
        origin="unknown", is_keeper=True, quality_known=True, lossless=True,
    )
    loser = DuplicateMember(
        group_id=group.id, sync_id="s2", path="loser.mp3", title="Song", artist="Artist",
        origin="musizerr", download_id=download.id, is_keeper=False, quality_known=True, lossless=False,
    )
    session.add(keeper)
    session.add(loser)
    session.commit()
    session.refresh(group)
    session.refresh(keeper)
    session.refresh(loser)
    return group, keeper, loser


# ------------------------------------------------------------------
# Logica di risoluzione — chiamata diretta della funzione route
# ------------------------------------------------------------------

@pytest.fixture
def db_engine():
    engine = new_engine()
    return engine


@pytest.fixture
def registry_and_config():
    storage = FakeStorageProvider("fakestorage")
    # Keyed sul path REALE (Download.file_path), mai sul path virtuale del
    # membro — vedi _make_group.
    storage.files["real/loser.mp3"] = b"loser-bytes"
    sync = FakeSyncProvider("fakesync")
    registry = FakeRegistry(storage=[storage], syncs=[sync])
    settings_engine = new_engine()
    config = ConfigManager(engine=settings_engine)
    config.update({"storage_provider": "fakestorage", "sync_provider": "fakesync"})
    return registry, config, storage, sync


def test_resolve_deletes_only_the_deletable_non_keeper_member(db_engine, registry_and_config):
    registry, config, storage, sync = registry_and_config
    with Session(db_engine) as session:
        group, keeper, loser = _make_group(session)

        result = resolve_duplicate_group(
            group.id, ResolveBody(keep_member_id=keeper.id), admin=None, session=session,
            registry=registry, config=config, auth_manager=_FakeAuthManager(),
        )

    assert result["deleted"] == ["real/loser.mp3"]  # path REALE, mai il virtuale "loser.mp3"
    assert storage.deleted == ["real/loser.mp3"]
    assert sync.scan_calls == 1  # rescan triggerato solo perche' qualcosa e' stato cancellato

    with Session(db_engine) as session:
        refreshed = session.get(DuplicateGroup, group.id)
        assert refreshed.status == "resolved"


def test_resolve_never_deletes_external_origin_member(db_engine, registry_and_config):
    registry, config, storage, sync = registry_and_config
    with Session(db_engine) as session:
        group = DuplicateGroup(recording_id="rec-2", status="needs_review", reason="external_or_unknown_origin_would_be_deleted")
        session.add(group)
        session.flush()
        keeper = DuplicateMember(group_id=group.id, sync_id="s1", path="keeper.flac", title="Song", artist="Artist",
                                  origin="musizerr", is_keeper=False, quality_known=True, lossless=True)
        external = DuplicateMember(group_id=group.id, sync_id="s2", path="external.mp3", title="Song", artist="Artist",
                                    origin="unknown", is_keeper=False, quality_known=True, lossless=False)
        session.add(keeper)
        session.add(external)
        session.commit()
        session.refresh(keeper)

        result = resolve_duplicate_group(
            group.id, ResolveBody(keep_member_id=keeper.id), admin=None, session=session,
            registry=registry, config=config, auth_manager=_FakeAuthManager(),
        )

    assert result["deleted"] == []
    assert result["skipped_non_deletable"] == ["external.mp3"]
    assert storage.deleted == []


def test_resolve_refuses_already_resolved_group(db_engine, registry_and_config):
    registry, config, _storage, _sync = registry_and_config
    from fastapi import HTTPException

    with Session(db_engine) as session:
        group, keeper, _loser = _make_group(session, status="resolved")
        with pytest.raises(HTTPException) as exc_info:
            resolve_duplicate_group(
                group.id, ResolveBody(keep_member_id=keeper.id), admin=None, session=session,
                registry=registry, config=config, auth_manager=_FakeAuthManager(),
            )
        assert exc_info.value.status_code == 409


def test_resolve_refuses_unknown_keep_member_id(db_engine, registry_and_config):
    registry, config, _storage, _sync = registry_and_config
    from fastapi import HTTPException

    with Session(db_engine) as session:
        group, _keeper, _loser = _make_group(session)
        with pytest.raises(HTTPException) as exc_info:
            resolve_duplicate_group(
                group.id, ResolveBody(keep_member_id="does-not-exist"), admin=None, session=session,
                registry=registry, config=config, auth_manager=_FakeAuthManager(),
            )
        assert exc_info.value.status_code == 400


def test_resolve_all_resolves_every_proposed_group_only(db_engine, registry_and_config):
    registry, config, storage, sync = registry_and_config
    storage.files["real/loser2.mp3"] = b"loser2-bytes"

    with Session(db_engine) as session:
        _make_group(session, status="proposed")
        group2 = DuplicateGroup(recording_id="rec-2", status="proposed", reason="best_quality")
        session.add(group2)
        session.flush()
        seed_parents(session, tracks=["t-2"])
        download2 = Download(track_id="t-2", status="downloaded", file_path="real/loser2.mp3")
        session.add(download2)
        session.flush()
        session.add(DuplicateMember(group_id=group2.id, sync_id="s3", path="keeper2.flac", title="Song",
                                     artist="Artist", origin="unknown", is_keeper=True, quality_known=True))
        session.add(DuplicateMember(group_id=group2.id, sync_id="s4", path="loser2.mp3", title="Song",
                                     artist="Artist", origin="musizerr", download_id=download2.id,
                                     is_keeper=False, quality_known=True))
        # Un gruppo needs_review non deve mai essere toccato da resolve-all.
        needs_review = DuplicateGroup(recording_id="rec-3", status="needs_review", reason="unknown_quality")
        session.add(needs_review)
        session.commit()
        needs_review_id = needs_review.id

        result = resolve_all_duplicate_groups(
            admin=None, session=session, registry=registry, config=config, auth_manager=_FakeAuthManager(),
        )

    assert len(result["resolved"]) == 2
    assert sorted(storage.deleted) == ["real/loser.mp3", "real/loser2.mp3"]
    assert sync.scan_calls == 1  # un solo rescan per l'intera resolve-all

    with Session(db_engine) as session:
        assert session.get(DuplicateGroup, needs_review_id).status == "needs_review"


def test_dismiss_marks_group_dismissed(db_engine, registry_and_config):
    with Session(db_engine) as session:
        group, _keeper, _loser = _make_group(session)
        result = dismiss_duplicate_group(group.id, admin=None, session=session)
    assert result["status"] == "dismissed"


def test_list_groups_returns_members(db_engine, registry_and_config):
    with Session(db_engine) as session:
        _make_group(session)
        response = get_duplicate_groups(admin=None, session=session, config=registry_and_config[1], status=None)
    assert len(response["groups"]) == 1
    assert len(response["groups"][0]["members"]) == 2


# ------------------------------------------------------------------
# Permessi reali — via TestClient (401/403), stesso stile di
# test_routines_api_permissions.py
# ------------------------------------------------------------------

@pytest.fixture
def perm_engine():
    engine = new_engine()
    return engine


@pytest.fixture
def perm_client(perm_engine):
    from routes.library_duplicates import router as duplicates_router

    config = ConfigManager(engine=perm_engine)
    config.update({"auth_enabled": True})

    app = FastAPI()
    app.include_router(duplicates_router)

    def _session_override():
        with Session(perm_engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: FakeRegistry()
    app.dependency_overrides[deps.get_auth_manager] = lambda: _FakeAuthManager()

    with TestClient(app) as c:
        yield c, perm_engine


def _make_user(engine, *, role: str) -> User:
    with Session(engine) as session:
        user = User(username=f"{role}-user-dup", password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _token_for(user: User) -> str:
    return create_access_token(user.id, user.role)


def test_anonymous_get_is_401(perm_client):
    c, _engine = perm_client
    resp = c.get("/library/duplicates")
    assert resp.status_code == 401


def test_non_admin_get_is_403(perm_client):
    c, engine = perm_client
    user = _make_user(engine, role="user")
    resp = c.get("/library/duplicates", headers={"Authorization": f"Bearer {_token_for(user)}"})
    assert resp.status_code == 403


def test_admin_get_is_200(perm_client):
    c, engine = perm_client
    admin = _make_user(engine, role="admin")
    resp = c.get("/library/duplicates", headers={"Authorization": f"Bearer {_token_for(admin)}"})
    assert resp.status_code == 200
    assert resp.json() == {"groups": []}


def test_non_admin_resolve_is_403(perm_client):
    c, engine = perm_client
    user = _make_user(engine, role="user")
    resp = c.post(
        "/library/duplicates/some-id/resolve",
        json={"keep_member_id": "x"},
        headers={"Authorization": f"Bearer {_token_for(user)}"},
    )
    assert resp.status_code == 403


def test_anonymous_resolve_all_is_401(perm_client):
    c, _engine = perm_client
    resp = c.post("/library/duplicates/resolve-all")
    assert resp.status_code == 401


# ------------------------------------------------------------------
# Dopo la cancellazione lo stato della libreria non punta più al file
# ------------------------------------------------------------------

def _resolve(session, group, keeper, registry, config):
    return resolve_duplicate_group(
        group.id, ResolveBody(keep_member_id=keeper.id), admin=None, session=session,
        registry=registry, config=config, auth_manager=_FakeAuthManager(),
    )


def test_a_deleted_file_is_no_longer_the_download_s_file_nor_a_library_entry(db_engine, registry_and_config):
    from db.models import LibraryEntry
    from sqlmodel import select

    registry, config, _storage, _sync = registry_and_config
    with Session(db_engine) as session:
        group, keeper, loser = _make_group(session)
        download_id = loser.download_id
        session.add(LibraryEntry(track_id=None, file_path="real/loser.mp3", file_format="mp3", storage_provider="fakestorage"))
        session.commit()
        _resolve(session, group, keeper, registry, config)

    with Session(db_engine) as session:
        assert session.get(Download, download_id).file_path is None
        assert session.exec(select(LibraryEntry)).all() == []


class _FlakyStorage(FakeStorageProvider):
    def __init__(self, failing: str) -> None:
        super().__init__("fakestorage")
        self.failing = failing

    def delete(self, relative_path, session):
        if relative_path == self.failing:
            raise OSError("550 permission denied")
        return super().delete(relative_path, session)


def test_a_resolve_that_fails_midway_keeps_what_it_deleted_and_can_be_retried(db_engine, registry_and_config):
    from fastapi import HTTPException

    _registry, config, _storage, sync = registry_and_config
    storage = _FlakyStorage(failing="real/second.mp3")
    registry = FakeRegistry(storage=[storage], syncs=[sync])
    with Session(db_engine) as session:
        group, keeper, loser = _make_group(session)
        loser_id, keeper_id, group_id = loser.id, keeper.id, group.id
        seed_parents(session, tracks=["t-2"])
        second_download = Download(track_id="t-2", status="downloaded", file_path="real/second.mp3")
        session.add(second_download)
        session.flush()
        session.add(DuplicateMember(
            group_id=group.id, sync_id="s3", path="second.mp3", title="Song", artist="Artist",
            origin="musizerr", download_id=second_download.id, quality_known=True, lossless=False,
        ))
        session.commit()

        with pytest.raises(HTTPException) as failure:
            _resolve(session, group, keeper, registry, config)
        assert failure.value.status_code == 502

    with Session(db_engine) as session:
        assert session.get(DuplicateMember, loser_id).deleted_at is not None
        assert session.get(DuplicateGroup, group_id).status == "proposed"

        storage.failing = None
        result = _resolve(session, session.get(DuplicateGroup, group_id), session.get(DuplicateMember, keeper_id), registry, config)

    assert storage.deleted == ["real/loser.mp3", "real/second.mp3"]  # il primo non due volte
    assert result["deleted"] == ["real/loser.mp3", "real/second.mp3"]
