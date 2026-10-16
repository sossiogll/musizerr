"""
The two routines of the playlists, on the real scheduler (``workers/scheduler.py``),
the real tasks (``workers/tasks.py``) and the real routines page:

- ``sync_playlists`` mirrors the playlists on the sync providers and FOLLOWS
  ``sync_library_index`` (a track downloaded meanwhile has an id there only
  after the index);
- ``refresh_followed_playlists`` looks at the followed playlists and starts the
  mirroring when something changed.

A failure is a failed run with a stable code, never ``ok`` with a warning.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

import db.engine as db_engine
from core import settings_catalog
from core.auth import AccountTarget
from core.auth_manager import AuthManager
from core.identifiers import sync_id_type
from musizerr_sdk.identifiers import Identifier
from core.routines import (
    SYNC_LIBRARY_INDEX, SYNC_PLAYLISTS, enabled_setting_key, get_routine, interval_setting_key,
)
from db import playlist_store
from db.auth_store import CredentialStore
from db.models import Playlist, PlaylistSync, RoutineRun, TrackRecord, User
from db.track_store import add_identifiers
from routes.system import routines as routines_route
from services import downloads, playlist_monitor, playlist_sync, playlists, routines as routine_service
from services.config_manager import ConfigManager
from workers import scheduler, tasks
from workers.celery_app import QUEUE_MAINTENANCE, queue_of

from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider
from tests.database import new_engine
from tests.playlists_support import PlaylistSyncProvider, Source

REFRESH = "refresh_followed_playlists"
SYNC = "fakesync"


@pytest.fixture
def engine(monkeypatch):
    eng = new_engine()
    for module in (scheduler, tasks):
        monkeypatch.setattr(module, "engine", eng)
    monkeypatch.setattr(db_engine, "engine", eng)
    monkeypatch.setattr(scheduler, "worker_alive", lambda queue: True)
    monkeypatch.setattr(tasks, "auth_manager_for", lambda store: FakeAuthManager())
    monkeypatch.setattr(tasks, "shared_store", lambda session: None)
    monkeypatch.setattr(playlist_sync, "auth_manager_for", lambda store: AuthManager(store, token_expiry_buffer=0))
    monkeypatch.setattr(playlist_monitor, "auth_manager_for", lambda store: FakeAuthManager())
    config = ConfigManager(engine=eng)
    config.update({"auth_enabled": False})
    monkeypatch.setattr(tasks, "get_config", lambda: config)
    monkeypatch.setattr(downloads, "get_config", lambda: config)
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: None)
    return eng


@pytest.fixture
def host(engine, monkeypatch, sent_tasks):
    sync, source = PlaylistSyncProvider(SYNC, library=[]), Source()
    state = SimpleNamespace(sync=sync, source=source, registry=FakeRegistry(metadata=[source], syncs=[sync]),
                            engine=engine)
    monkeypatch.setattr(tasks, "get_registry", lambda: state.registry)
    # The routines of the chain; the others would be due as well.
    monkeypatch.setattr(scheduler, "ROUTINES", tuple(get_routine(k) for k in (SYNC_LIBRARY_INDEX, SYNC_PLAYLISTS, REFRESH)))
    handlers = {
        "workers.routines.run": lambda kwargs: scheduler.run_routine(**kwargs),
        "workers.routines.start": lambda kwargs: scheduler.start(**kwargs),
    }

    def drain() -> list[tuple[str, str, str | None]]:
        done = []
        while sent_tasks:
            name, kwargs, options = sent_tasks.pop(0)
            done.append((name, kwargs.get("routine_key", ""), options.get("queue")))
            handlers[name](kwargs)
        return done

    state.drain = drain
    return state


def _user_with_playlist(engine, name="alice", *, indexed=("One",)) -> str:
    with Session(engine) as session:
        user = User(username=name, password_hash="x")
        session.add(user)
        session.commit()
        CredentialStore(session, user_id=user.id).save_credentials(
            SYNC, {"username": f"{name}-on-server", "password": "pw"}, AccountTarget.PERSONAL)
        record = TrackRecord(title="One", artist="Artist", album="Album")
        session.add(record)
        session.commit()
        if "One" in indexed:
            add_identifiers(record.internal_id, [Identifier(type=sync_id_type(SYNC), value="s-One")], session)
            session.commit()
        playlist = playlists.create(session, user.id, "Drive")
        playlist_store.append_tracks(session, playlist.id, [record.internal_id])
        session.commit()
        return user.id


def _runs(engine, key: str) -> list[RoutineRun]:
    with Session(engine) as session:
        return list(session.exec(select(RoutineRun).where(RoutineRun.routine_key == key)
                                 .order_by(RoutineRun.queued_at)).all())


def _page(engine) -> dict[str, dict]:
    with Session(engine) as session:
        admin = User(username="admin", password_hash="x", role="admin")
        session.add(admin)
        session.commit()
        page = routines_route.list_routines(admin, ConfigManager(engine=engine), FakeRegistry(), session)
        session.delete(admin)
        session.commit()
    return {entry["key"]: entry for entry in page["routines"]}


# ---------------------------------------------------------------------------
# What they are
# ---------------------------------------------------------------------------


def test_the_mirroring_follows_the_library_index_and_the_refresh_has_its_own_period():
    mirror, refresh = get_routine(SYNC_PLAYLISTS), get_routine(REFRESH)

    assert (mirror.task_name, mirror.follows, mirror.default_interval_minutes, mirror.min_interval_minutes) == (
        "workers.tasks.sync_playlists", SYNC_LIBRARY_INDEX, 60, 5)
    assert (refresh.task_name, refresh.follows, refresh.default_interval_minutes, refresh.min_interval_minutes) == (
        "workers.tasks.refresh_followed_playlists", None, 60, 5)
    assert mirror.default_enabled and refresh.default_enabled
    assert mirror.reports_progress and refresh.reports_progress
    assert queue_of(mirror.task_name) == queue_of(refresh.task_name) == QUEUE_MAINTENANCE
    keys = {s.key for s in settings_catalog.CATALOG}
    for routine in (mirror, refresh):
        assert {enabled_setting_key(routine.key), interval_setting_key(routine.key)} <= keys


def test_the_deletion_task_is_routed_to_the_maintenance_queue():
    assert queue_of("workers.maintenance.delete_playlist_tracks") == QUEUE_MAINTENANCE


# ---------------------------------------------------------------------------
# The mirroring
# ---------------------------------------------------------------------------


def test_the_mirroring_waits_for_the_index_and_runs_right_after_it(engine, host):
    _user_with_playlist(engine)

    assert scheduler.tick()["dispatched"] == [SYNC_LIBRARY_INDEX, REFRESH]  # the mirroring waits for the index
    executed = host.drain()

    assert [(name, key) for name, key, _ in executed] == [
        ("workers.routines.run", SYNC_LIBRARY_INDEX), ("workers.routines.run", REFRESH),
        ("workers.routines.run", SYNC_PLAYLISTS)]
    index, = _runs(engine, SYNC_LIBRARY_INDEX)
    mirror, = _runs(engine, SYNC_PLAYLISTS)
    assert mirror.started_at >= index.finished_at


def test_a_run_mirrors_every_playlist_and_says_so_per_provider(engine, host):
    _user_with_playlist(engine)

    scheduler.dispatch_routine(get_routine(SYNC_PLAYLISTS))
    host.drain()

    last = _page(engine)[SYNC_PLAYLISTS]["last_run"]
    assert last["status"] == "ok" and last["error_code"] is None
    assert [(p["sync_provider"], p["created"], p["failed"]) for p in last["result"]["providers"]] == [(SYNC, 1, 0)]
    assert host.sync.calls == [("create", "Drive", ["s-One"])]
    with Session(engine) as session:
        assert len(session.exec(select(PlaylistSync)).all()) == 1


def test_an_unreachable_server_is_a_failed_run_with_its_code(engine, host):
    _user_with_playlist(engine)
    host.sync.fail = True

    scheduler.dispatch_routine(get_routine(SYNC_PLAYLISTS))
    host.drain()

    last = _page(engine)[SYNC_PLAYLISTS]["last_run"]
    assert (last["status"], last["error_code"]) == ("error", "sync_provider_unreachable")
    assert last["result"]["failed_providers"] == [SYNC]


def test_an_edit_event_starts_the_mirroring_and_the_event_while_it_is_queued_adds_nothing(engine, host):
    _user_with_playlist(engine)

    assert routine_service.start_routine(SYNC_PLAYLISTS) is True
    executed = host.drain()

    assert [(name, key) for name, key, _ in executed] == [
        ("workers.routines.start", SYNC_PLAYLISTS), ("workers.routines.run", SYNC_PLAYLISTS)]
    assert [r.status for r in _runs(engine, SYNC_PLAYLISTS)] == ["ok"]


def test_the_switch_turns_the_mirroring_off(engine, host):
    from db.settings_store import SettingsRepository

    with Session(engine) as session:
        SettingsRepository(session).set_system(enabled_setting_key(SYNC_PLAYLISTS), False)

    assert scheduler.tick()["dispatched"] == [SYNC_LIBRARY_INDEX, REFRESH]
    host.drain()
    assert _runs(engine, SYNC_PLAYLISTS) == []


# ---------------------------------------------------------------------------
# Watching the followed playlists
# ---------------------------------------------------------------------------


def _follow(engine, host) -> str:
    with Session(engine) as session:
        owner = User(username="carol", password_hash="x")
        session.add(owner)
        session.commit()
        result = playlists.follow(
            session, owner, host.source.get_playlist("p1", None), config=ConfigManager(engine=engine),
            registry=host.registry, auth_manager=FakeAuthManager())
        return result.playlist.id


def test_a_change_of_the_source_is_applied_and_starts_the_mirroring(engine, host, sent_tasks):
    playlist_id = _follow(engine, host)
    sent_tasks.clear()
    host.source.tracks = host.source.tracks[:2]
    host.source.snapshot = "v2"

    scheduler.dispatch_routine(get_routine(REFRESH))
    executed = host.drain()

    last = _page(engine)[REFRESH]["last_run"]
    assert last["status"] == "ok"
    assert (last["result"]["playlists"], last["result"]["changed"], last["result"]["removed_tracks"]) == (1, 1, 1)
    # The change reaches the sync providers: the refresh asked for the mirroring, which then ran.
    assert [(name, key) for name, key, _ in executed] == [
        ("workers.routines.run", REFRESH), ("workers.routines.start", SYNC_PLAYLISTS),
        ("workers.routines.run", SYNC_PLAYLISTS)]
    with Session(engine) as session:
        assert playlist_store.count(session, playlist_id) == 2


def test_a_source_that_fails_is_a_failed_run_with_the_code(engine, host):
    _follow(engine, host)
    host.source.fail_ids = {"p1"}

    scheduler.dispatch_routine(get_routine(REFRESH))
    host.drain()

    last = _page(engine)[REFRESH]["last_run"]
    assert (last["status"], last["error_code"]) == ("error", "metadata_provider_unreachable")
    assert [f["error_code"] for f in last["result"]["failed"]] == ["metadata_provider_unreachable"]
    with Session(engine) as session:
        assert session.exec(select(Playlist.refresh_error_code)).one() == "metadata_provider_unreachable"


def test_the_run_shows_where_it_is_while_it_works(engine, host, monkeypatch):
    _follow(engine, host)
    reports = []
    original = playlist_monitor.refresh_all

    def spying(session, registry, config, *, progress=None):
        return original(session, registry, config, progress=lambda *a, **kw: reports.append((a, kw)))

    monkeypatch.setattr(playlist_monitor, "refresh_all", spying)
    scheduler.dispatch_routine(get_routine(REFRESH))
    host.drain()

    assert reports == [(("refreshing",), {"provider": "catalog", "processed": 0, "total": 1})]


def test_a_sync_provider_that_keeps_no_playlists_is_never_asked(engine, host):
    host.registry = FakeRegistry(syncs=[FakeSyncProvider("plain")])
    _user_with_playlist(engine)

    scheduler.dispatch_routine(get_routine(SYNC_PLAYLISTS))
    host.drain()

    last = _page(engine)[SYNC_PLAYLISTS]["last_run"]
    assert last["status"] == "ok" and last["result"]["providers"] == []
