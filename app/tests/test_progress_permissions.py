"""
``GET /progress`` (SSE dei download): autenticato come l'audio completo
(cookie di stream, oppure bearer per i client API: scelta deliberata, vedi
``routes/deps.get_stream_user``) e filtrato per utente con la stessa regola
di ``GET /downloads``. Il filtro è provato come generatore alimentato da
messaggi finti: nessun Redis.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import anyio
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core import user_auth
from core.user_auth import create_access_token, create_stream_token
from db.engine import get_session
from db.models import SYSTEM_USER_ID, Download, DownloadFollower, TrackRecord, User
from routes import deps
from routes.system import progress
from routes.system.progress import KEEPALIVE_FRAME, router as progress_router, sse_frames
from services.config_manager import ConfigManager



def _user(engine, name: str, role: str = "user") -> User:
    with Session(engine) as session:
        user = User(username=name, password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _download(engine, requested_by: str | None, followers: tuple[str, ...] = ()) -> str:
    with Session(engine) as session:
        track = TrackRecord(title="T", artist="A", album="B")
        session.add(track)
        session.commit()
        dl = Download(track_id=track.internal_id, requested_by=requested_by, status="downloading")
        session.add(dl)
        session.commit()
        for follower in followers:
            session.add(DownloadFollower(download_id=dl.id, user_id=follower))
        session.commit()
        return dl.id


def _event(download_id: str, **extra) -> str:
    return json.dumps({"download_id": download_id, "status": "downloading", **extra})


async def _feed(messages):
    for message in messages:
        yield message


def _frames(messages, user, is_visible) -> list[str]:
    async def collect():
        return [frame async for frame in sse_frames(_feed(messages), user, is_visible)]
    return asyncio.run(collect())


@pytest.fixture
def world(engine, monkeypatch):
    """Due utenti, un admin e tre download: di alice, di bob (seguito da
    alice) e di bob soltanto. La visibilità reale legge questo DB."""
    monkeypatch.setattr(progress, "engine", engine)
    alice = _user(engine, "alice")
    bob = _user(engine, "bob")
    admin = _user(engine, "root", role="admin")
    own = _download(engine, alice.id)
    followed = _download(engine, bob.id, followers=(alice.id,))
    foreign = _download(engine, bob.id)
    return {"alice": alice, "bob": bob, "admin": admin, "own": own, "followed": followed, "foreign": foreign}


def _visible_ids(frames: list[str]) -> set[str]:
    return {json.loads(f[len("data: "):])["download_id"] for f in frames if f.startswith("data: ")}


def test_a_user_receives_only_its_own_and_followed_downloads(world):
    alice = world["alice"]
    messages = [_event(world[k]) for k in ("own", "followed", "foreign")]

    frames = _frames(messages, alice, progress._download_visibility(alice))

    assert _visible_ids(frames) == {world["own"], world["followed"]}


def test_an_admin_receives_everything(world):
    admin = world["admin"]
    messages = [_event(world[k]) for k in ("own", "followed", "foreign")]

    frames = _frames(messages, admin, progress._download_visibility(admin))

    assert _visible_ids(frames) == {world["own"], world["followed"], world["foreign"]}


def test_with_auth_disabled_the_built_in_admin_receives_everything(world):
    system = deps._SYSTEM_USER
    assert system.id == SYSTEM_USER_ID and system.role == "admin"
    messages = [_event(world[k]) for k in ("own", "foreign")]

    frames = _frames(messages, system, progress._download_visibility(system))

    assert _visible_ids(frames) == {world["own"], world["foreign"]}


def test_an_event_addressed_to_a_user_reaches_only_that_user(world):
    alice, bob = world["alice"], world["bob"]
    message = _event(f"lib-dl-sync:{alice.id}", user_id=alice.id)

    assert _visible_ids(_frames([message], alice, lambda _id: False)) == {f"lib-dl-sync:{alice.id}"}
    assert _frames([message], bob, lambda _id: True) == []


def test_an_event_with_no_download_and_no_addressee_is_for_admins_only(world):
    message = json.dumps({"status": "downloading"})

    assert _frames([message], world["alice"], lambda _id: True) == []
    assert len(_frames([message], world["admin"], lambda _id: False)) == 1


def test_keepalive_frames_flow_for_an_authorized_client(world):
    frames = _frames([None, _event(world["foreign"]), None], world["alice"], lambda _id: False)

    assert frames == [KEEPALIVE_FRAME, KEEPALIVE_FRAME]


def test_malformed_messages_are_skipped(world):
    assert _frames(["not json", "[1, 2]"], world["admin"], lambda _id: True) == []


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_follower_added_after_the_stream_opened_is_seen_once_the_hidden_result_expires(world, engine, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(progress.time, "monotonic", clock)
    carol = _user(engine, "carol")
    is_visible = progress._download_visibility(carol)
    assert is_visible(world["foreign"]) is False

    with Session(engine) as session:
        session.add(DownloadFollower(download_id=world["foreign"], user_id=carol.id))
        session.commit()

    assert is_visible(world["foreign"]) is False  # ancora nella finestra
    clock.now += progress._HIDDEN_TTL_SECONDS + 1
    assert is_visible(world["foreign"]) is True


def test_events_of_someone_else_s_download_do_not_query_the_db_each_time(world, engine, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(progress.time, "monotonic", clock)
    queries = []
    real_session = progress.Session
    monkeypatch.setattr(progress, "Session", lambda *a, **k: queries.append(1) or real_session(*a, **k))
    is_visible = progress._download_visibility(_user(engine, "dave"))

    for _ in range(10):
        assert is_visible(world["foreign"]) is False

    assert len(queries) == 1


class _FakePubSub:
    def __init__(self, closed: list) -> None:
        self.closed = closed

    async def subscribe(self, channel):
        return None

    async def get_message(self, **kwargs):
        await anyio.sleep_forever()

    async def aclose(self):
        await anyio.sleep(0)
        self.closed.append("pubsub")


class _FakeRedis:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def pubsub(self):
        return _FakePubSub(self.closed)

    async def aclose(self):
        await anyio.sleep(0)
        self.closed.append("client")


def test_a_client_that_disconnects_releases_its_redis_connection(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(progress.aioredis, "from_url", lambda url: fake)

    async def scenario():
        async def consume():
            async for _ in progress._redis_messages():
                pass

        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            await anyio.sleep(0.01)
            group.cancel_scope.cancel()  # come Starlette alla disconnessione

    anyio.run(scenario)

    assert fake.closed == ["pubsub", "client"]


# ---------------------------------------------------------------------------
# Autenticazione della rotta (TestClient): solo i rifiuti, un client
# autorizzato aprirebbe lo stream Redis vero.
# ---------------------------------------------------------------------------


@pytest.fixture
def client(engine):
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})
    app = FastAPI()
    app.include_router(progress_router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    with TestClient(app) as c:
        yield c


def test_a_guest_gets_401(client):
    assert client.get("/progress").status_code == 401


def test_an_expired_stream_cookie_gets_401(client, engine):
    alice = _user(engine, "alice")
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    expired = jwt.encode(
        {"sub": alice.id, "type": "stream", "iat": past, "exp": past + timedelta(minutes=1)},
        user_auth._get_secret(), algorithm=user_auth._ALGORITHM,
    )
    client.cookies.set(user_auth.STREAM_COOKIE_NAME, expired)

    assert client.get("/progress").status_code == 401


def test_an_invalid_stream_cookie_gets_401(client):
    client.cookies.set(user_auth.STREAM_COOKIE_NAME, "not-a-token")

    assert client.get("/progress").status_code == 401


def test_an_access_token_in_the_cookie_gets_401(client, engine):
    alice = _user(engine, "alice")
    client.cookies.set(user_auth.STREAM_COOKIE_NAME, create_access_token(alice.id, alice.role))

    assert client.get("/progress").status_code == 401


def test_a_stream_token_as_bearer_gets_401(client, engine):
    alice = _user(engine, "alice")

    resp = client.get("/progress", headers={"Authorization": f"Bearer {create_stream_token(alice.id)}"})

    assert resp.status_code == 401


def test_a_bearer_access_token_is_accepted_for_api_clients(engine):
    """Deliberato e documentato in get_stream_user: i client API usano il
    bearer, il browser il cookie. Provato sulla dipendenza per non aprire lo
    stream Redis."""
    alice = _user(engine, "alice")
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})

    class _Request:
        cookies: dict = {}

    class _Credentials:
        credentials = create_access_token(alice.id, alice.role)

    with Session(engine) as session:
        resolved = deps.get_stream_user(_Request(), session, config, credentials=_Credentials())
    assert resolved.id == alice.id
