"""
A notification of the external system drives a delegated download to its
end, through the whole host path: the generic webhook route
(``routes/delegation_webhook``) finds the download by the handle the provider
extracts from the payload, queues a reconciliation tick, and the tick
(``workers.maintenance.reconcile_delegation``) judges the REQUESTED track by
the files the delegate lists (``services.delegation``), not by the package as
a whole. Real cases (ADR-0001): downloads stuck in "dispatched" because the
rest of the album was never complete, and a delegate that was unreachable at
the moment of the notification.

Generic fakes only: the delegate keeps the tracks it has a file for in a list
and can be made unreachable.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlmodel import Session

from core import download_states as ds
from db.models import Download, TrackRecord
from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizeer_sdk.delegation import DelegationStatus
from routes.delegation_webhook import delegation_webhook
from services.config_manager import ConfigManager
from workers import maintenance

from tests.conftest import FakeDownloadProvider, FakeRedis, FakeRegistry, make_track
from tests.database import new_engine

HANDLE = "package-123"
TOKEN = "s3cr3t"


class _Delegate(FakeDownloadProvider):
    """An external system that holds a package (an album) and says, as data,
    which tracks of it already have a file."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self) -> None:
        super().__init__("delegate")
        self.has_file: list = []
        self.reachable = True
        self.package_state = ds.DOWNLOADING

    def delegation_event_handle(self, payload):
        return str(payload["package"]) if isinstance(payload, dict) and payload.get("event") == "imported" else None

    def list_delegated_tracks(self, handle, session):
        if not self.reachable:
            raise ConnectionError("unreachable")
        return list(self.has_file)

    def poll_delegation(self, handle, session):
        if not self.reachable:
            raise ConnectionError("unreachable")
        return DelegationStatus(state=self.package_state)


class _Request:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self):
        return self._payload


class _Auth:
    def __init__(self, credential_store=None) -> None:
        pass

    def authenticate(self, provider):
        return AuthSession.anonymous()


@pytest.fixture
def world(monkeypatch):
    engine = new_engine()
    config = ConfigManager(engine=new_engine())
    config.update({"delegation_webhook_enabled": True, "delegation_webhook_token": TOKEN})
    delegate = _Delegate()
    registry = FakeRegistry(downloads=[delegate])
    pending: list[dict] = []
    monkeypatch.setattr(maintenance, "engine", engine)
    monkeypatch.setattr(maintenance, "publish_download_progress", lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "_redis_client", FakeRedis())
    monkeypatch.setattr(maintenance, "auth_manager_for", lambda store: _Auth(store))
    monkeypatch.setattr(maintenance, "get_config", lambda: config)
    monkeypatch.setattr(maintenance, "get_registry", lambda: registry)
    # The ticks are only captured, never run on their own: a tick that fails
    # transiently queues itself again, and running it at once would loop.
    monkeypatch.setattr(
        maintenance.reconcile_delegation, "apply_async",
        lambda kwargs, countdown=None: pending.append(dict(kwargs)),
    )

    def drain() -> None:
        todo, pending[:] = list(pending), []
        for kwargs in todo:
            maintenance.reconcile_delegation(**kwargs)

    def notify(session) -> dict:
        return asyncio.run(delegation_webhook(
            "delegate", _Request({"event": "imported", "package": HANDLE}), config, session, registry, token=TOKEN,
        ))

    return type("World", (), dict(
        engine=engine, delegate=delegate, pending=pending, drain=staticmethod(drain), notify=staticmethod(notify),
    ))


def _dispatched(engine, track) -> str:
    with Session(engine) as session:
        record = TrackRecord(title=track.title, artist=track.artist, album=track.album, duration_ms=track.duration_ms)
        session.add(record)
        session.commit()
        download = Download(track_id=record.internal_id, status=ds.DISPATCHED, delegation_handle=HANDLE,
                            provider_used="delegate")
        session.add(download)
        session.commit()
        return download.id


def _status(engine, download_id: str) -> str:
    with Session(engine) as session:
        return session.get(Download, download_id).status


REQUESTED = make_track(title="Requested Song", artist="Artist X", album="Album X", duration_ms=200_000)


def test_a_notification_lands_the_requested_track_though_the_rest_of_the_package_is_incomplete(world):
    world.delegate.has_file = [make_track(title="Requested Song", artist="Artist X", album="", duration_ms=200_000)]
    download_id = _dispatched(world.engine, REQUESTED)

    with Session(world.engine) as session:
        result = world.notify(session)

    assert result["ok"] is True and result["triggered"] == 1
    world.drain()
    assert _status(world.engine, download_id) == ds.DOWNLOADED


def test_a_notification_for_a_package_that_does_not_have_the_track_does_not_land_it(world):
    world.delegate.has_file = [make_track(title="Another Song", artist="Artist X", album="", duration_ms=200_000)]
    download_id = _dispatched(world.engine, REQUESTED)

    with Session(world.engine) as session:
        world.notify(session)
    world.drain()

    assert _status(world.engine, download_id) != ds.DOWNLOADED


def test_an_unreachable_delegate_reschedules_without_finalizing_and_the_next_tick_recovers(world):
    world.delegate.reachable = False
    download_id = _dispatched(world.engine, REQUESTED)

    with Session(world.engine) as session:
        world.notify(session)
    assert [kw["download_id"] for kw in world.pending] == [download_id]
    world.drain()

    assert _status(world.engine, download_id) not in ds.TERMINAL  # never finalized on an unreachable delegate
    assert [kw["download_id"] for kw in world.pending] == [download_id]  # it queued itself again

    world.delegate.reachable = True
    world.delegate.has_file = [make_track(title="Requested Song", artist="Artist X", album="", duration_ms=200_000)]
    world.drain()

    assert _status(world.engine, download_id) == ds.DOWNLOADED
