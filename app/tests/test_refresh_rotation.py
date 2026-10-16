"""
``POST /auth/account/refresh``: rotation with a short grace window.

Every tab of the browser shares ONE refresh token (localStorage). When two
of them refresh at the same moment, the second presents the token the first
just rotated: before the grace window the second got a 401 and the frontend
signed the user out. Inside the window the server answers the same new pair;
outside it, re-presenting a rotated token is real reuse and revokes the whole
family of tokens of that sign-in.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import main
from core import user_auth
from db.engine import get_session
from db.models import RefreshToken, User
from routes import deps
from services import refresh_tokens
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry
from tests.database import new_engine


@pytest.fixture
def api():
    # Not the shared in-memory connection: the concurrent test needs real
    # connections competing for the write lock, as in production.
    engine = new_engine(addressable=True)
    with Session(engine) as session:
        session.add(User(username="alice", password_hash=user_auth.hash_password("pw"), role="user"))
        session.commit()
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
    client.engine = engine
    yield client
    for key in overrides:
        main.app.dependency_overrides.pop(key, None)


def _login(client) -> str:
    resp = client.post("/auth/account/login", json={"username": "alice", "password": "pw"})
    assert resp.status_code == 200
    return resp.json()["refresh_token"]


def _refresh(client, token):
    return client.post("/auth/account/refresh", json={"refresh_token": token})


def _age_rotation(client, token: str, seconds: float) -> None:
    """Moves the rotation of ``token`` ``seconds`` into the past."""
    with Session(client.engine) as s:
        row = s.exec(select(RefreshToken).where(RefreshToken.token_hash == user_auth.hash_refresh_token(token))).one()
        row.revoked_at = row.revoked_at - timedelta(seconds=seconds)
        s.add(row)
        s.commit()


def test_a_second_tab_presenting_the_just_rotated_token_gets_the_same_new_pair(api):
    token = _login(api)

    first = _refresh(api, token)
    second = _refresh(api, token)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["refresh_token"] == first.json()["refresh_token"]
    # The pair keeps working: the new token rotates normally.
    assert _refresh(api, first.json()["refresh_token"]).status_code == 200


def test_concurrent_refreshes_with_the_same_token_all_succeed_with_one_pair(api):
    token = _login(api)
    results: list = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        results.append(_refresh(api, token))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert [r.status_code for r in results] == [200] * 4
    assert len({r.json()["refresh_token"] for r in results}) == 1
    with Session(api.engine) as s:
        # One login token (rotated) and one successor: never a fork.
        rows = s.exec(select(RefreshToken)).all()
    assert len(rows) == 2
    assert sum(1 for r in rows if r.revoked_at is None) == 1


def test_reuse_outside_the_grace_window_revokes_the_whole_family(api):
    token = _login(api)
    successor = _refresh(api, token).json()["refresh_token"]
    _age_rotation(api, token, refresh_tokens.REUSE_GRACE_SECONDS + 1)

    replay = _refresh(api, token)

    assert replay.status_code == 401
    # The legitimate holder is signed out as well: the family is compromised.
    assert _refresh(api, successor).status_code == 401


def test_a_token_revoked_by_logout_is_never_replayed(api):
    token = _login(api)
    api.post("/auth/account/logout", json={"refresh_token": token})

    assert _refresh(api, token).status_code == 401


def test_a_replay_after_the_successor_was_logged_out_is_refused(api):
    token = _login(api)
    successor = _refresh(api, token).json()["refresh_token"]
    api.post("/auth/account/logout", json={"refresh_token": successor})

    assert _refresh(api, token).status_code == 401


def test_the_successor_is_never_stored_in_clear(api):
    token = _login(api)
    successor = _refresh(api, token).json()["refresh_token"]

    with Session(api.engine) as s:
        stored = {r.token_hash for r in s.exec(select(RefreshToken)).all()}
    assert successor not in stored and token not in stored
    assert user_auth.hash_refresh_token(successor) in stored


def test_an_unknown_token_is_401(api):
    _login(api)
    assert _refresh(api, "nope").status_code == 401


def test_an_expired_token_is_401(api):
    token = _login(api)
    with Session(api.engine) as s:
        row = s.exec(select(RefreshToken)).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.add(row)
        s.commit()
    assert _refresh(api, token).status_code == 401
