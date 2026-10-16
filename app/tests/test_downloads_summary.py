"""
``GET /downloads/summary`` e ``GET /downloads?phase=``: i contatori della
pagina Download e della Home contano le tracce per stato AGGREGATO (il più
avanti fra i tentativi che chi chiama vede) e per fase, con la stessa
visibilità di ``GET /downloads``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import Session

import main
from core import download_states as ds
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import Download, TrackRecord, User
from routes import deps
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry
from tests.database import new_engine
from tests.test_openapi_responses import SPEC, assert_conforms

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def api():
    engine = new_engine()
    with Session(engine) as session:
        alice = User(username="alice", password_hash="x", role="user")
        bob = User(username="bob", password_hash="x", role="user")
        admin = User(username="root", password_hash="x", role="admin")
        session.add_all([alice, bob, admin])
        session.commit()

        def track(title, *attempts):
            record = TrackRecord(title=title, artist="A", album="B")
            session.add(record)
            session.flush()
            for status, who, completed in attempts:
                session.add(Download(track_id=record.internal_id, status=status, requested_by=who.id,
                                     started_at=NOW, completed_at=completed))

        # alice: fallito poi riuscito (→ done, questa settimana), in coda, fallito, conflitto
        track("retried", ("failed", alice, None), ("downloaded", alice, NOW - timedelta(days=1)))
        track("waiting", ("pending", alice, None))
        track("broken", ("failed", alice, None))
        track("dupe", ("conflict", alice, None))
        track("old", ("downloaded", alice, NOW - timedelta(days=30)))
        # bob: in download
        track("bobs", ("downloading", bob, None))
        session.commit()
        tokens = {u.username: create_access_token(u.id, u.role) for u in (alice, bob, admin)}

    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})

    def _session():
        with Session(engine) as s:
            yield s

    overrides = {
        get_session: _session,
        deps.get_config: lambda: config,
        deps.get_registry: lambda: FakeRegistry(),
        deps.get_auth_manager: lambda: FakeAuthManager(),
    }
    main.app.dependency_overrides.update(overrides)
    client = TestClient(main.app)

    def get(url, who=None):
        headers = {"Authorization": f"Bearer {tokens[who]}"} if who else {}
        return client.get(url, headers=headers)

    yield get
    for key in overrides:
        main.app.dependency_overrides.pop(key, None)


def test_a_user_counts_their_own_tracks_by_aggregated_state(api):
    resp = api("/downloads/summary", "alice")

    assert resp.status_code == 200
    assert_conforms(resp, "GET", "/downloads/summary")
    body = resp.json()
    assert body["statuses"] == {**{s: 0 for s in ds.ORDER}, "downloaded": 2, "pending": 1, "failed": 1, "conflict": 1}
    assert body["phases"] == {"queued": 1, "active": 0, "done": 2, "failed": 1, "review": 1}
    assert body["total"] == 5
    assert body["downloaded_since"] == 1  # "old" è di un mese fa


def test_an_admin_counts_the_whole_instance(api):
    body = api("/downloads/summary", "root").json()

    assert body["total"] == 6 and body["phases"]["active"] == 1


def test_the_window_is_configurable(api):
    assert api("/downloads/summary?days=60", "alice").json()["downloaded_since"] == 2


def test_a_guest_gets_401(api):
    assert api("/downloads/summary").status_code == 401


@pytest.mark.parametrize("phase, titles", [
    ("done", {"retried", "old"}),
    ("failed", {"broken"}),
    ("review", {"dupe"}),
    ("queued", {"waiting"}),
    ("active", set()),
])
def test_the_phase_filter_uses_the_aggregated_state(api, phase, titles):
    resp = api(f"/downloads?phase={phase}", "alice")

    assert resp.status_code == 200
    assert_conforms(resp, "GET", "/downloads")
    body = resp.json()
    assert {item["track"]["title"] for item in body["items"]} == titles
    assert body["total"] == len(titles)


def test_several_phases_at_once(api):
    body = api("/downloads?phase=failed&phase=review&phase=queued", "alice").json()
    assert {item["track"]["title"] for item in body["items"]} == {"broken", "dupe", "waiting"}
    assert body["total"] == 3


def test_an_unknown_phase_is_rejected(api):
    assert api("/downloads?phase=nope", "alice").status_code == 422


def test_the_spec_lists_the_domain_phases():
    assert SPEC["components"]["schemas"]["DownloadPhase"]["enum"] == list(ds.PHASES)
