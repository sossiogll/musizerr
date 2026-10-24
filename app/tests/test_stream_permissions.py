"""Copre l'accesso ai flussi audio COMPLETI di `routes/stream.py` (vedi
docs/permissions.md, voce spostata da "Gaps left open" alla sezione
"Closed"): un anonimo con `auth_enabled=True` non deve mai poter raggiungere
`/stream/...`, ma un client API con un bearer access token si', e un cookie
di streaming ottenuto da `POST /auth/account/stream-session` pure. Un access
token non e' uno stream token e viceversa: due `type` JWT distinti apposta
(`core/user_auth.py`) cosi' non sono intercambiabili.

`GET /playback/...` segue la stessa regola sul lato "cosa offrire": un
anonimo vede solo la preview (o "none"), mai lo stream_url completo.

Regola d'oro rispettata: nessun provider reale, nessuna route chiamata via
rete — le funzioni delle route sono richiamate direttamente con
dipendenze finte/isolate, stesso stile di `test_history_permissions.py`.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlmodel import Session

from musizeer_sdk.auth import AuthSession
from services.config_manager import ConfigManager
from musizeer_sdk.identifiers import Identifier
from musizeer_sdk.models import Track
from core.serialization import serialize_track
from core.user_auth import create_access_token, create_stream_token, hash_password
from db.models import Download, TrackIdentifierRecord, TrackRecord
from db.user_service import create_user
from routes.deps import get_stream_user
from routes.stream import get_playback_info

from tests.conftest import FakeRegistry, FakeStorageProvider


# ------------------------------------------------------------------
# Fakes locali — solo per questi test, nessun provider reale.
# ------------------------------------------------------------------

class _FakeRequest:
    """Stand-in minimale per fastapi.Request: get_stream_user legge solo
    ``.cookies``."""

    def __init__(self, cookies: dict[str, str] | None = None):
        self.cookies = cookies or {}


class _FakeAuthManager:
    """Autentica sempre in modo anonimo — basta a esercitare storage.exists()."""

    def authenticate(self, provider):
        return AuthSession.anonymous()


def _fake_storage(existing: set[str] | None = None) -> FakeStorageProvider:
    storage = FakeStorageProvider("fakestorage")
    storage.files.update({path: b"" for path in existing or ()})
    return storage


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def config(engine):
    def _make(**overrides):
        cm = ConfigManager(engine=engine)
        data = {"auth_enabled": True, "storage_provider": "fakestorage"}
        data.update(overrides)
        cm.update(data)
        return cm
    return _make


def _make_user(session, *, role: str = "user", username: str = "alice"):
    return create_user(session, username=username, password_hash=hash_password("x"), role=role)


# ------------------------------------------------------------------
# get_stream_user — la dipendenza dei route audio completi
# ------------------------------------------------------------------

def test_stream_anonymous_rejected_when_auth_enabled(config, session):
    cfg = config()
    with pytest.raises(HTTPException) as exc:
        get_stream_user(_FakeRequest(), session, cfg, credentials=None)
    assert exc.value.status_code == 401


def test_stream_valid_cookie_allowed(config, session):
    cfg = config()
    user = _make_user(session)
    token = create_stream_token(user.id)
    resolved = get_stream_user(_FakeRequest({"musizeer_stream_token": token}), session, cfg, credentials=None)
    assert resolved.id == user.id


def test_stream_access_token_in_cookie_rejected(config, session):
    """Un access token piazzato nel cookie di streaming non deve funzionare —
    tipo JWT diverso apposta."""
    cfg = config()
    user = _make_user(session)
    token = create_access_token(user.id, user.role)
    with pytest.raises(HTTPException) as exc:
        get_stream_user(_FakeRequest({"musizeer_stream_token": token}), session, cfg, credentials=None)
    assert exc.value.status_code == 401


def test_stream_token_as_bearer_rejected(config, session):
    """Reciproco: uno stream token passato come bearer non e' un access
    token valido."""
    cfg = config()
    user = _make_user(session)
    token = create_stream_token(user.id)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with pytest.raises(HTTPException) as exc:
        get_stream_user(_FakeRequest(), session, cfg, credentials=creds)
    assert exc.value.status_code == 401


def test_stream_valid_bearer_access_token_allowed(config, session):
    """Un client API senza cookie puo' ancora usare il bearer access token."""
    cfg = config()
    user = _make_user(session)
    token = create_access_token(user.id, user.role)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    resolved = get_stream_user(_FakeRequest(), session, cfg, credentials=creds)
    assert resolved.id == user.id


def test_stream_auth_disabled_allows_anonymous(config, session):
    cfg = config(auth_enabled=False)
    resolved = get_stream_user(_FakeRequest(), session, cfg, credentials=None)
    assert resolved.username == "system"


# ------------------------------------------------------------------
# GET /playback/... — un anonimo con auth attiva vede solo la preview
# ------------------------------------------------------------------

def _seed_track(session, *, provider="fake", provider_id="abc", preview_url="http://preview/x.mp3") -> str:
    track = Track(
        title="Song",
        artist="Artist",
        album="Album",
        duration_ms=200_000,
        provider=provider,
        provider_id=provider_id,
        preview_url=preview_url,
        external_ids=[Identifier(type="provider_id", value=provider_id, provider=provider)],
    )
    record = TrackRecord(
        title=track.title,
        artist=track.artist,
        album=track.album,
        duration_ms=track.duration_ms,
        track_data=json.dumps(serialize_track(track)),
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    session.add(TrackIdentifierRecord(
        track_id=record.internal_id, id_type=f"{provider}_id", value=provider_id,
    ))
    session.commit()
    return record.internal_id


def test_playback_anonymous_gets_preview_never_full(config, session):
    cfg = config()  # auth_enabled=True, un download completo esiste comunque
    fake_storage = _fake_storage()
    registry = FakeRegistry(storage=[fake_storage])
    track_id = _seed_track(session)
    session.add(Download(track_id=track_id, status="downloaded", file_path="song.mp3"))
    session.commit()
    # Il file esiste davvero nello storage — se l'anonimo lo vedesse sarebbe
    # un vero leak, non solo un placeholder vuoto.
    fake_storage.files["song.mp3"] = b""

    result = get_playback_info(
        "fake", "abc", session, cfg, registry,
        auth_manager=_FakeAuthManager(), user=None,
    )
    assert result == {"type": "preview", "stream_url": "http://preview/x.mp3"}


def test_playback_anonymous_no_preview_reports_none(config, session):
    cfg = config()
    registry = FakeRegistry()
    _seed_track(session, preview_url=None)

    result = get_playback_info(
        "fake", "abc", session, cfg, registry,
        auth_manager=_FakeAuthManager(), user=None,
    )
    assert result == {"type": "none", "stream_url": None}


def test_playback_signed_in_user_gets_full(config, session):
    cfg = config()
    fake_storage = _fake_storage({"song.mp3"})
    registry = FakeRegistry(storage=[fake_storage])
    user = _make_user(session)
    track_id = _seed_track(session)
    session.add(Download(track_id=track_id, status="downloaded", file_path="song.mp3"))
    session.commit()

    result = get_playback_info(
        "fake", "abc", session, cfg, registry,
        auth_manager=_FakeAuthManager(), user=user,
    )
    assert result == {"type": "full", "stream_url": f"/api/stream/{track_id}"}


def test_playback_auth_disabled_allows_anonymous_full(config, session):
    cfg = config(auth_enabled=False)
    fake_storage = _fake_storage({"song.mp3"})
    registry = FakeRegistry(storage=[fake_storage])
    track_id = _seed_track(session)
    session.add(Download(track_id=track_id, status="downloaded", file_path="song.mp3"))
    session.commit()

    result = get_playback_info(
        "fake", "abc", session, cfg, registry,
        auth_manager=_FakeAuthManager(), user=None,
    )
    assert result == {"type": "full", "stream_url": f"/api/stream/{track_id}"}
