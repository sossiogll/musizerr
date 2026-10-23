"""
Open mode (authentication off): the instance has ONE owner, the synthetic user
(``__system__``), and for it "my own account" and "the instance's shared account"
are the same account. The rule lives in one place (``services/credentials.py``)
and holds only while ``auth_enabled`` is off.

Seen on a real install: the admin connected the sync provider from
Administration > Providers (a shared account), the likes sync looked for a
personal one, found none, and the periodic sync dispatched to nobody (the
synthetic user is not active).
"""
from __future__ import annotations

import json
import time

import pytest
from sqlmodel import Session, select

from core.auth import AccountTarget
from core.auth_manager import AuthManager
from core.identifiers import sync_id_type
from db import library_job_store as job_store
from db.auth_store import CredentialStore
from db.models import (
    SYSTEM_USER_ID,
    LibraryJobRun,
    Playlist,
    PlaylistSync,
    ProviderCredential,
    TrackRecord,
    User,
    UserSavedTrack,
)
from db.settings_store import SettingsRepository
from db.track_store import add_identifiers
from musizerr_sdk.auth import (
    AccountModel,
    AuthDefinition,
    AuthFlow,
    BearerHeaderRequestAuth,
    ClientCredentialsFallback,
    OAuth2Config,
)
from musizerr_sdk.identifiers import Identifier
from db import playlist_store
from services import library_jobs, playlist_monitor, playlist_sync
from services.credentials import (
    account_holders,
    has_own_account,
    holds_accounts,
    open_mode,
    own_store,
    shared_store,
    user_store,
)
from services.linked_accounts import linked_accounts

from tests.conftest import FakeAuthManager, FakeMetadataProvider, FakeRegistry, FakeSyncProvider, make_track
from tests.database import seed_system_user
from tests.loopback import as_proxy
from tests.playlists_support import PlaylistSyncProvider, Source

PERSONAL, SHARED = AccountTarget.PERSONAL, AccountTarget.SHARED
SYSTEM = SYSTEM_USER_ID
LOGIN = {"username": "owner-on-server", "password": "pw"}


def _token(access: str) -> dict:
    return {"access_token": access, "obtained_at": time.time(), "expires_in": 3600}


@pytest.fixture
def session(engine):
    seed_system_user(engine)
    with Session(engine) as s:
        yield s


def _authentication(session: Session, enabled: bool) -> None:
    SettingsRepository(session).set_system("auth_enabled", enabled)
    session.commit()


@pytest.fixture
def open_instance(session):
    _authentication(session, False)
    return session


@pytest.fixture
def closed_instance(session):
    _authentication(session, True)
    return session


def _user(session: Session, name: str, *, active: bool = True) -> str:
    user = User(username=name, password_hash="x", is_active=active)
    session.add(user)
    session.commit()
    return user.id


def _oauth_provider(model: AccountModel, *, fallback: bool = False):
    provider = FakeMetadataProvider("acct")
    provider.auth = AuthDefinition(
        flow=AuthFlow.OAUTH2,
        account_model=model,
        oauth2=OAuth2Config(
            authorize_url="https://example.invalid/authorize", token_url="https://example.invalid/token",
            scopes=[], request_auth=BearerHeaderRequestAuth(),
            fallback_auth=ClientCredentialsFallback() if fallback else None,
        ),
    )
    return as_proxy(provider)


def _header(store: CredentialStore, provider) -> str | None:
    from core.auth_manager import ProviderNotConfigured

    try:
        return AuthManager(store, token_expiry_buffer=0).authenticate(provider).headers["Authorization"]
    except ProviderNotConfigured:
        return None


# ---------------------------------------------------------------------------
# Open mode: the synthetic user's own account is the shared one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", [AccountModel.PERSONAL, AccountModel.PERSONAL_WITH_SHARED])
def test_in_open_mode_the_owners_own_account_is_the_shared_one(open_instance, model):
    session = open_instance
    shared_store(session).save_oauth2_token("acct", _token("shared"), SHARED)
    provider = _oauth_provider(model)

    assert _header(user_store(session, SYSTEM), provider) == "Bearer shared"
    assert _header(own_store(session, SYSTEM), provider) == "Bearer shared"
    assert has_own_account(own_store(session, SYSTEM), provider) is True


def test_in_open_mode_an_own_only_store_still_never_sees_the_app_only_token(open_instance):
    session = open_instance
    shared_store(session).save_oauth2_app_token("acct", _token("app-only"))
    provider = _oauth_provider(AccountModel.PERSONAL_WITH_SHARED, fallback=True)

    assert _header(own_store(session, SYSTEM), provider) is None
    assert _header(user_store(session, SYSTEM), provider) == "Bearer app-only"


def test_in_open_mode_a_system_model_account_is_not_the_owners_own(open_instance):
    session = open_instance
    shared_store(session).save_oauth2_token("acct", _token("shared"), SHARED)

    assert _header(own_store(session, SYSTEM), _oauth_provider(AccountModel.SYSTEM)) is None
    assert _header(user_store(session, SYSTEM), _oauth_provider(AccountModel.SYSTEM)) == "Bearer shared"


def test_in_open_mode_a_personal_write_lands_on_the_shared_row(open_instance):
    session = open_instance

    user_store(session, SYSTEM).save_credentials("sync", LOGIN, PERSONAL, external_id="signs-in-as-nobody")

    rows = session.exec(select(ProviderCredential)).all()
    assert [(row.user_id, row.provider, row.external_id) for row in rows] == [(None, "sync", None)]
    assert shared_store(session).shared_credentials("sync").data == LOGIN
    assert own_store(session, SYSTEM).own_credentials("sync").data == LOGIN


def test_in_open_mode_both_pages_show_the_one_connection_and_disconnect_removes_it(open_instance):
    session = open_instance
    provider = FakeMetadataProvider("sync")
    provider.auth = AuthDefinition(flow=AuthFlow.CREDENTIALS, account_model=AccountModel.PERSONAL_WITH_SHARED)
    registry = FakeRegistry(metadata=[as_proxy(provider)])
    shared_store(session).save_credentials("sync", LOGIN, SHARED)

    (entry,) = linked_accounts(registry, session, SYSTEM)
    assert entry["personal"]["connected"] is True and entry["personal"]["username"] == "owner-on-server"
    assert entry["shared"]["connected"] is True

    # Disconnecting "my" account is disconnecting the instance's: there is one.
    own_store(session, SYSTEM).delete_credentials("sync", PERSONAL)
    (entry,) = linked_accounts(registry, session, SYSTEM)
    assert entry["personal"]["connected"] is False and entry["shared"]["connected"] is False


def test_in_open_mode_the_owner_signs_nobody_in(open_instance):
    session = open_instance
    store = user_store(session, SYSTEM)

    assert store.instance_owner is True
    assert store.own_external_id("sync") is None


def test_revoking_every_personal_account_never_touches_the_shared_ones(open_instance):
    session = open_instance
    shared_store(session).save_credentials("sync", LOGIN, SHARED)
    shared_store(session).save_api_key("keyed", "k")

    assert own_store(session, SYSTEM).delete_all_personal() == 0

    assert shared_store(session).shared_credentials("sync") is not None
    assert shared_store(session).get_api_key("keyed") == "k"


# ---------------------------------------------------------------------------
# Authentication ON: the rule never applies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", [AccountModel.PERSONAL, AccountModel.PERSONAL_WITH_SHARED])
def test_with_authentication_on_a_users_own_store_never_reads_the_shared_row(closed_instance, model):
    session = closed_instance
    alice = _user(session, "alice")
    shared_store(session).save_oauth2_token("acct", _token("shared"), SHARED)
    shared_store(session).save_oauth2_app_token("acct", _token("app-only"))
    provider = _oauth_provider(model, fallback=True)

    assert _header(own_store(session, alice), provider) is None
    assert has_own_account(own_store(session, alice), provider) is False
    # And the shared row is not their account to write either.
    own_store(session, alice).save_oauth2_token("acct", _token("alice"), PERSONAL)
    assert {row.user_id for row in session.exec(select(ProviderCredential)).all() if row.credential_type == "oauth2_token"} == {None, alice}


def test_with_authentication_on_the_synthetic_user_is_nobody(closed_instance):
    session = closed_instance
    shared_store(session).save_oauth2_token("acct", _token("shared"), SHARED)
    shared_store(session).save_credentials("sync", LOGIN, SHARED)
    provider = _oauth_provider(AccountModel.PERSONAL_WITH_SHARED)

    assert user_store(session, SYSTEM).instance_owner is False
    assert own_store(session, SYSTEM).instance_owner is False
    assert _header(own_store(session, SYSTEM), provider) is None
    # A personal write for it would be a row of its own (nobody reads it), never the shared one.
    own_store(session, SYSTEM).delete_credentials("sync", PERSONAL)
    assert shared_store(session).shared_credentials("sync") is not None


def test_a_switch_on_takes_effect_for_the_next_store(session):
    shared_store(session).save_credentials("sync", LOGIN, SHARED)
    provider = FakeMetadataProvider("sync")
    provider.auth = AuthDefinition(flow=AuthFlow.CREDENTIALS, account_model=AccountModel.PERSONAL_WITH_SHARED)
    provider = as_proxy(provider)

    _authentication(session, False)
    assert has_own_account(own_store(session, SYSTEM), provider) is True
    _authentication(session, True)
    assert has_own_account(own_store(session, SYSTEM), provider) is False


def test_a_setting_that_cannot_be_read_counts_as_authentication_on(session, monkeypatch):
    _authentication(session, False)

    def unreadable(self, key, default=None):
        raise RuntimeError("database gone")

    monkeypatch.setattr(SettingsRepository, "get_system", unreadable)

    assert open_mode(session) is False
    assert user_store(session, SYSTEM).instance_owner is False


def test_a_failing_read_of_the_setting_leaves_the_callers_pending_work_alone(session, engine, monkeypatch):
    """``open_mode`` does not own the session: a job that wrote rows and then builds a store
    must still be able to commit them when the read of the setting fails (on PostgreSQL a
    failed statement would abort the whole transaction without a savepoint)."""
    import sqlalchemy as sa

    session.add(User(id="pending-user", username="pending-user", password_hash="x"))

    def broken(self, key, default=None):
        self._session.exec(sa.text("SELECT * FROM no_such_table"))

    monkeypatch.setattr(SettingsRepository, "get_system", broken)

    assert open_mode(session) is False
    assert user_store(session, SYSTEM).instance_owner is False
    session.commit()  # the pending write is still there, and still commits

    with Session(engine) as check:
        assert check.get(User, "pending-user") is not None


def test_an_instance_never_configured_is_open(session):
    assert open_mode(session) is True


# ---------------------------------------------------------------------------
# Who the host's per-user jobs act for
# ---------------------------------------------------------------------------


def test_in_open_mode_the_jobs_act_for_the_instance_alone(open_instance):
    session = open_instance
    alice = _user(session, "alice")

    assert [u.id for u in account_holders(session)] == [SYSTEM]
    assert holds_accounts(session, session.get(User, SYSTEM)) is True
    assert holds_accounts(session, session.get(User, alice)) is True
    assert session.get(User, SYSTEM).is_active is False  # never made active to get there


def test_with_authentication_on_the_jobs_act_for_the_active_users_never_the_system_user(closed_instance):
    session = closed_instance
    alice = _user(session, "alice")
    _user(session, "bob", active=False)

    assert [u.id for u in account_holders(session)] == [alice]
    assert holds_accounts(session, session.get(User, SYSTEM)) is False
    assert holds_accounts(session, None) is False


# ---------------------------------------------------------------------------
# The likes sync
# ---------------------------------------------------------------------------


class _Likes(FakeMetadataProvider):
    """A provider with a likes library behind a login (model personal-with-shared)."""

    auth = AuthDefinition(flow=AuthFlow.CREDENTIALS, account_model=AccountModel.PERSONAL_WITH_SHARED)

    def __init__(self, tracks=()):
        super().__init__("likes")
        self._liked = list(tracks)

    def supports_user_library(self):
        return True

    def get_saved_tracks(self, session, limit=50, offset=0):
        return self._liked[offset:offset + limit]


@pytest.fixture
def jobs(engine, monkeypatch):
    """The worker side of the library jobs, without Redis or a broker."""
    from workers import tasks

    monkeypatch.setattr(tasks, "engine", engine)
    monkeypatch.setattr(library_jobs, "publish_event", lambda event: None)
    monkeypatch.setattr(library_jobs, "worker_alive", lambda queue: True)
    monkeypatch.setattr(tasks, "auth_manager_for", lambda store: AuthManager(store, token_expiry_buffer=0))
    monkeypatch.setattr(tasks.routines, "start_routine", lambda key: True)
    dispatched: list[dict] = []
    monkeypatch.setattr(library_jobs, "_dispatch", lambda name, kwargs, task_id: dispatched.append(kwargs))
    monkeypatch.setattr(tasks, "dispatched", dispatched, raising=False)
    return tasks


def _registry(monkeypatch, tasks, *providers):
    monkeypatch.setattr(tasks, "get_registry", lambda: FakeRegistry(metadata=list(providers)))


def _run_of(engine, user_id: str) -> LibraryJobRun:
    with Session(engine) as session:
        return session.exec(select(LibraryJobRun).where(LibraryJobRun.user_id == user_id)).one()


def test_the_manual_likes_sync_of_the_owner_imports_with_the_shared_account(engine, open_instance, jobs, monkeypatch):
    session = open_instance
    liked = [make_track(f"Song {i}", provider="likes", provider_id=f"l{i}") for i in range(3)]
    provider = _Likes(liked)
    _registry(monkeypatch, jobs, as_proxy(provider))
    shared_store(session).save_credentials("likes", LOGIN, SHARED)  # connected from Administration > Providers
    run = job_store.create_run(session, SYSTEM, "library_sync")

    outcome = jobs.sync_saved_tracks_for_user.run(user_id=SYSTEM, run_id=run.id)

    done = _run_of(engine, SYSTEM)
    assert outcome == {"status": "done", "total_synced": 3} and done.status == "completed"
    assert json.loads(done.result) == {"tracks_synced": 3}
    with Session(engine) as check:
        assert len(check.exec(select(UserSavedTrack).where(UserSavedTrack.user_id == SYSTEM)).all()) == 3


def test_a_likes_sync_with_nothing_connected_ends_failed_not_completed_with_zero(engine, open_instance, jobs, monkeypatch):
    session = open_instance
    _registry(monkeypatch, jobs, as_proxy(_Likes()))
    run = job_store.create_run(session, SYSTEM, "library_sync")

    jobs.sync_saved_tracks_for_user.run(user_id=SYSTEM, run_id=run.id)

    done = _run_of(engine, SYSTEM)
    assert done.status == "failed" and done.error_code == library_jobs.PROVIDER_NOT_CONNECTED
    assert done.error_message == library_jobs.MESSAGES[library_jobs.PROVIDER_NOT_CONNECTED]


def test_a_likes_sync_with_no_provider_that_keeps_likes_ends_failed_with_its_own_code(engine, open_instance, jobs, monkeypatch):
    session = open_instance
    _registry(monkeypatch, jobs, as_proxy(FakeMetadataProvider("plain")))
    run = job_store.create_run(session, SYSTEM, "library_sync")

    outcome = jobs.sync_saved_tracks_for_user.run(user_id=SYSTEM, run_id=run.id)

    done = _run_of(engine, SYSTEM)
    assert outcome["status"] == "failed"
    assert done.status == "failed" and done.error_code == library_jobs.NO_LIBRARY_PROVIDER
    assert done.error_message == library_jobs.MESSAGES[library_jobs.NO_LIBRARY_PROVIDER]
    assert json.loads(done.result or "{}") == {}


def test_with_authentication_on_a_users_likes_sync_never_uses_the_shared_account(engine, closed_instance, jobs, monkeypatch):
    session = closed_instance
    alice = _user(session, "alice")
    _registry(monkeypatch, jobs, as_proxy(_Likes([make_track("Song", provider="likes", provider_id="l1")])))
    shared_store(session).save_credentials("likes", LOGIN, SHARED)
    run = job_store.create_run(session, alice, "library_sync")

    jobs.sync_saved_tracks_for_user.run(user_id=alice, run_id=run.id)

    done = _run_of(engine, alice)
    assert done.status == "failed" and done.error_code == library_jobs.PROVIDER_NOT_CONNECTED
    with Session(engine) as check:
        assert check.exec(select(UserSavedTrack)).all() == []


def test_the_periodic_sync_dispatches_the_owner_in_open_mode(engine, open_instance, jobs, monkeypatch):
    session = open_instance
    _registry(monkeypatch, jobs, as_proxy(_Likes()))
    shared_store(session).save_credentials("likes", LOGIN, SHARED)

    result = jobs.sync_all_users_saved_tracks.run()

    assert result == {"dispatched": 1, "users": 1, "skipped_not_connected": 0}
    assert [call["user_id"] for call in jobs.dispatched] == [SYSTEM]


def test_the_periodic_sync_skips_the_owner_with_nothing_connected(engine, open_instance, jobs, monkeypatch):
    _registry(monkeypatch, jobs, as_proxy(_Likes()))

    result = jobs.sync_all_users_saved_tracks.run()

    assert result == {"dispatched": 0, "users": 1, "skipped_not_connected": 1}
    assert jobs.dispatched == []


def test_the_periodic_sync_never_dispatches_the_system_user_with_authentication_on(engine, closed_instance, jobs, monkeypatch):
    session = closed_instance
    alice = _user(session, "alice")
    _registry(monkeypatch, jobs, as_proxy(_Likes()))
    shared_store(session).save_credentials("likes", LOGIN, SHARED)
    user_store(session, alice).save_credentials("likes", LOGIN, PERSONAL)

    result = jobs.sync_all_users_saved_tracks.run()

    assert result == {"dispatched": 1, "users": 1, "skipped_not_connected": 0}
    assert [call["user_id"] for call in jobs.dispatched] == [alice]


# ---------------------------------------------------------------------------
# The playlists mirrored on the sync provider
# ---------------------------------------------------------------------------


def _playlist_of_the_owner(session: Session, sync_name: str = "fakesync") -> None:
    record = TrackRecord(title="One", artist="Artist", album="Album")
    session.add(record)
    session.commit()
    add_identifiers(record.internal_id, [Identifier(type=sync_id_type(sync_name), value="s-One")], session)
    session.commit()
    playlist = Playlist(user_id=SYSTEM, name="Drive")
    session.add(playlist)
    session.flush()
    playlist_store.append_tracks(session, playlist.id, [record.internal_id])
    session.commit()


def test_in_open_mode_the_playlists_go_to_the_shared_account(open_instance, monkeypatch):
    session = open_instance
    monkeypatch.setattr(playlist_sync, "auth_manager_for", lambda store: AuthManager(store, token_expiry_buffer=0))
    sync = PlaylistSyncProvider()
    shared_store(session).save_credentials("fakesync", LOGIN, SHARED)
    _playlist_of_the_owner(session)

    (report,) = playlist_sync.push_all(session, FakeRegistry(syncs=[sync]))

    assert (report["users"], report["created"], report["skipped_no_account"]) == (1, 1, 0)
    assert sync.playlists["remote-1"]["account"] == "owner-on-server"
    assert session.exec(select(PlaylistSync)).one().user_id == SYSTEM


def test_with_authentication_on_the_playlists_of_a_leftover_system_user_go_nowhere(closed_instance, monkeypatch):
    session = closed_instance
    monkeypatch.setattr(playlist_sync, "auth_manager_for", lambda store: AuthManager(store, token_expiry_buffer=0))
    sync = PlaylistSyncProvider()
    shared_store(session).save_credentials("fakesync", LOGIN, SHARED)
    _playlist_of_the_owner(session)

    (report,) = playlist_sync.push_all(session, FakeRegistry(syncs=[sync]))

    assert (report["users"], report["created"], report["skipped_no_account"]) == (0, 0, 1)
    assert sync.calls == []


# ---------------------------------------------------------------------------
# The followed playlists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("authentication, looked_at", [(False, True), (True, False)])
def test_the_followed_playlists_of_the_owner_are_looked_at_only_in_open_mode(session, monkeypatch, authentication, looked_at):
    _authentication(session, authentication)
    source = Source()
    followed = Playlist(user_id=SYSTEM, name="Mirror", kind="followed", source_provider=source.name, source_id="p1")
    session.add(followed)
    session.commit()
    seen = []
    monkeypatch.setattr(playlist_monitor, "auth_manager_for", lambda store: FakeAuthManager())
    monkeypatch.setattr(playlist_monitor.playlists, "refresh", lambda *args, **kwargs: seen.append(args[1].id) or "refreshed")

    outcome = playlist_monitor._refresh_one(session, FakeRegistry(metadata=[source]), None, followed)

    assert (outcome == "refreshed") is looked_at
    assert seen == ([SYSTEM] if looked_at else [])


# ---------------------------------------------------------------------------
# The heart: a pending like, a like taken back, on the sync provider
# ---------------------------------------------------------------------------


class _Sync(FakeSyncProvider):
    """A sync provider with a login (personal-with-shared) that records likes and unlikes."""

    auth = AuthDefinition(flow=AuthFlow.CREDENTIALS, account_model=AccountModel.PERSONAL_WITH_SHARED)

    def __init__(self):
        super().__init__("nav")
        self.unliked: list[str] = []

    def unlike_track(self, sync_id, session):
        self.unliked.append(sync_id)


@pytest.fixture
def maintenance_host(engine, monkeypatch):
    from types import SimpleNamespace

    from workers import maintenance

    sync = _Sync()
    registry = FakeRegistry(syncs=[as_proxy(sync)])
    monkeypatch.setattr(maintenance, "engine", engine)
    monkeypatch.setattr(maintenance, "get_registry", lambda: registry)
    monkeypatch.setattr(maintenance, "get_config", lambda: SimpleNamespace(get=lambda k, d=None: d))
    monkeypatch.setattr(maintenance, "auth_manager_for", lambda store: AuthManager(store, token_expiry_buffer=0))
    monkeypatch.setattr(maintenance.reconcile_like, "apply_async", lambda kwargs, countdown=None: None)
    return SimpleNamespace(maintenance=maintenance, sync=sync)


def _landed_track(session: Session, user_id: str) -> tuple[str, str]:
    """A track on the sync provider (it has its id there) and a pending like of ``user_id`` for it."""
    from core import download_states as ds
    from db.models import Download, PendingLike

    record = TrackRecord(title="One", artist="Artist", album="Album")
    session.add(record)
    session.commit()
    add_identifiers(record.internal_id, [Identifier(type=sync_id_type("nav"), value="s-1")], session)
    session.add(Download(track_id=record.internal_id, status=ds.DOWNLOADED, file_path="a/1.flac"))
    marker = PendingLike(user_id=user_id, track_id=record.internal_id, sync_provider="nav")
    session.add(marker)
    session.commit()
    return marker.id, record.internal_id


def test_in_open_mode_the_pending_like_is_applied_with_the_shared_account(open_instance, maintenance_host):
    session = open_instance
    shared_store(session).save_credentials("nav", LOGIN, SHARED)
    marker_id, _track = _landed_track(session, SYSTEM)

    result = maintenance_host.maintenance.reconcile_like.run(pending_like_id=marker_id)

    assert result["status"] == "liked"
    assert maintenance_host.sync.liked == ["s-1"]


def test_in_open_mode_a_like_taken_back_is_taken_back_on_the_sync_provider(open_instance, maintenance_host):
    session = open_instance
    shared_store(session).save_credentials("nav", LOGIN, SHARED)
    _marker, track = _landed_track(session, SYSTEM)

    result = maintenance_host.maintenance.propagate_unlike.run(SYSTEM, track, "nav")

    assert result == {"status": "unliked"}
    assert maintenance_host.sync.unliked == ["s-1"]


def test_with_authentication_on_a_leftover_system_marker_never_uses_the_shared_account(closed_instance, maintenance_host):
    session = closed_instance
    shared_store(session).save_credentials("nav", LOGIN, SHARED)
    marker_id, track = _landed_track(session, SYSTEM)

    liked = maintenance_host.maintenance.reconcile_like.run(pending_like_id=marker_id)
    unliked = maintenance_host.maintenance.propagate_unlike.run(SYSTEM, track, "nav")

    assert liked["status"] == "no-personal-account" and unliked == {"status": "no-personal-account"}
    assert maintenance_host.sync.liked == [] and maintenance_host.sync.unliked == []


# ---------------------------------------------------------------------------
# "Download the ones liked elsewhere"
# ---------------------------------------------------------------------------


def test_in_open_mode_the_owners_likes_are_downloaded_without_a_request(engine, open_instance, jobs, monkeypatch):
    from services import downloads
    from services.config_manager import ConfigManager

    session = open_instance
    queued = []
    monkeypatch.setattr(jobs, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(jobs, "get_config", lambda: ConfigManager(engine=engine))
    monkeypatch.setattr(downloads, "get_config", lambda: ConfigManager(engine=engine))
    monkeypatch.setattr(downloads, "enqueue_track", lambda track, session, **kw: (
        queued.append(track.title), downloads.EnqueueResult(downloads.QUEUED, "t", "d"))[1])
    for i in range(2):
        record = TrackRecord(title=f"T{i}", artist="A", album="B",
                             track_data=json.dumps({"title": f"T{i}", "artist": "A", "album": "B", "duration_ms": 1}))
        session.add(record)
        session.flush()
        session.add(UserSavedTrack(user_id=SYSTEM, track_id=record.internal_id))
    session.commit()
    run = job_store.create_run(session, SYSTEM, "download_sync", params={"time_range": "all"})

    jobs.library_download_sync.run(user_id=SYSTEM, time_range="all", run_id=run.id)

    done = _run_of(engine, SYSTEM)
    assert done.status == "completed" and sorted(queued) == ["T0", "T1"]
    assert json.loads(done.result)["queued"] == 2
