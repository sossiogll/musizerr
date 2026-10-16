"""Copre i due gap di permessi chiusi in `routes/history.py` (vedi
`docs/permissions.md`):

1. `GET /downloads` non aveva alcuna dipendenza utente: chiunque poteva
   leggere la cronologia download dell'intera istanza, incluso chi aveva
   richiesto ciascun item. Ora richiede `CurrentUserDep` e, per un utente
   non-admin, filtra su `Download.requested_by` (colonna indicizzata) OPPURE
   su un aggancio registrato in `DownloadFollower` (vedi
   `attach_to_open_download`, `db/download_follower_store.py`).
2. `POST /downloads/{track_id}/retry` segue la stessa regola delle quattro
   route `/download/*` (`DownloaderDep`): serve un utente autenticato
   quando l'autenticazione è attiva.

Regola d'oro rispettata: nessun provider reale. Segue lo stile già in uso in
`test_delegation_webhook.py` e `test_config_route.py` — le route sono
funzioni Python ordinarie, richiamate direttamente con dipendenze finte/
isolate, non tramite TestClient/app reale (che userebbe l'engine e il DB
reali del container).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from services.config_manager import ConfigManager
from core.user_auth import decode_access_token, hash_password
from db.download_store import add_follower
from db.models import SYSTEM_USER_ID, Download, TrackRecord, User
from db.user_service import create_user
from routes.deps import Downloader, get_current_user
from services.capabilities import Viewer
from routes.history import get_downloads, retry_download

from tests.conftest import FakeRegistry


# ------------------------------------------------------------------
# Fixtures — isolated in-memory DB + isolated ConfigManager per test,
# come test_delegation_webhook.py / test_config_route.py.
# ------------------------------------------------------------------

@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def config(engine):
    def _make(**overrides):
        cm = ConfigManager(engine=engine)
        data = {"auth_enabled": True}
        data.update(overrides)
        cm.update(data)
        return cm
    return _make


@pytest.fixture
def registry():
    return FakeRegistry()


def _make_user(session, *, role: str = "user", username: str = "alice") -> User:
    return create_user(session, username=username, password_hash=hash_password("x"), role=role)


def _make_track_with_downloads(session, *, requested_by_pairs) -> str:
    """Crea una TrackRecord e N Download con i requested_by dati.
    ``requested_by_pairs`` e' una lista di (status, requested_by)."""
    track = TrackRecord(title="Song", artist="Artist", album="Album")
    session.add(track)
    session.commit()
    session.refresh(track)
    for status, requested_by in requested_by_pairs:
        dl = Download(track_id=track.internal_id, status=status, requested_by=requested_by)
        session.add(dl)
    session.commit()
    return track.internal_id


# ------------------------------------------------------------------
# GET /downloads — la dipendenza sottostante rifiuta l'anonimo
# ------------------------------------------------------------------

def test_current_user_dependency_rejects_anonymous_when_auth_enabled(config, session):
    cfg = config()
    with pytest.raises(HTTPException) as exc:
        get_current_user(session, cfg, credentials=None)
    assert exc.value.status_code == 401


def test_current_user_dependency_serves_signed_in_user(config, session):
    cfg = config()
    user = _make_user(session, role="user")
    from fastapi.security import HTTPAuthorizationCredentials
    from core.user_auth import create_access_token

    token = create_access_token(user.id, user.role)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    resolved = get_current_user(session, cfg, credentials=creds)
    assert resolved.id == user.id
    # sanity: il token decodifica sull'utente giusto (non solo "non lancia")
    assert decode_access_token(token)["sub"] == user.id


# ------------------------------------------------------------------
# GET /downloads — scoping admin vs non-admin
# ------------------------------------------------------------------

def test_get_downloads_admin_sees_every_requester(session, registry):
    admin = _make_user(session, role="admin", username="admin-1")
    other = _make_user(session, role="user", username="bob")
    _make_track_with_downloads(session, requested_by_pairs=[("downloaded", other.id)])

    result = get_downloads(session, registry, auth_manager=object(), viewer=GUEST, user=admin, page=1, limit=50, status=None, q=None, sort="recent")

    assert result["total"] == 1
    assert result["items"][0]["requested_by"] == "bob"


def test_get_downloads_non_admin_sees_only_own_items(session, registry):
    alice = _make_user(session, role="user", username="alice")
    bob = _make_user(session, role="user", username="bob")
    _make_track_with_downloads(session, requested_by_pairs=[("downloaded", alice.id)])
    _make_track_with_downloads(session, requested_by_pairs=[("downloaded", bob.id)])

    result = get_downloads(session, registry, auth_manager=object(), viewer=GUEST, user=alice, page=1, limit=50, status=None, q=None, sort="recent")

    assert result["total"] == 1
    assert result["items"][0]["requested_by"] == "alice"


def test_get_downloads_non_admin_does_not_see_other_users_attempts_on_shared_track(session, registry):
    # Stesso track, un tentativo di alice e uno di bob: alice non deve vedere
    # l'attempt di bob (ne' il suo requested_by) nella propria risposta.
    alice = _make_user(session, role="user", username="alice")
    bob = _make_user(session, role="user", username="bob")
    track_id = _make_track_with_downloads(
        session, requested_by_pairs=[("failed", bob.id), ("downloaded", alice.id)]
    )

    result = get_downloads(session, registry, auth_manager=object(), viewer=GUEST, user=alice, page=1, limit=50, status=None, q=None, sort="recent")

    assert result["total"] == 1
    item = result["items"][0]
    assert item["track_id"] == track_id
    assert len(item["attempts"]) == 1
    assert item["requested_by"] == "alice"


# ------------------------------------------------------------------
# POST /downloads/{track_id}/retry — stessa regola delle route /download/*
# (DownloaderDep: un guest riceve 401 prima di arrivare qui, vedi
# test_download_permissions.py). Qui: attribuzione e 404.
# ------------------------------------------------------------------

GUEST = Viewer(user_id=None, stream_full=False)
_SYSTEM = User(id=SYSTEM_USER_ID, username="system", password_hash="", role="admin")


def _seed_track(session) -> str:
    track = TrackRecord(title="Song", artist="Artist", album="Album")
    session.add(track)
    session.commit()
    session.refresh(track)
    return track.internal_id


def test_retry_is_attributed_to_the_signed_in_user(session, monkeypatch):
    user = _make_user(session, role="user")
    track_id = _seed_track(session)
    session.add(Download(track_id=track_id, status="failed", requested_by=user.id))  # a download of theirs to retry
    session.commit()

    calls = []
    monkeypatch.setattr("services.downloads.queue_download", lambda **kw: calls.append(kw))

    result = retry_download(track_id, session, user=Downloader(user), registry=FakeRegistry(), config=SimpleNamespace(auth_enabled=True), add_to_favorites=None, force=False)

    assert "download_id" in result
    assert calls and calls[0]["user_id"] == user.id
    dl = session.get(Download, result["download_id"])
    assert dl.requested_by == user.id


def test_retry_with_auth_disabled_has_no_requester(session, monkeypatch):
    track_id = _seed_track(session)

    calls = []
    monkeypatch.setattr("services.downloads.queue_download", lambda **kw: calls.append(kw))

    result = retry_download(track_id, session, user=Downloader(_SYSTEM), registry=FakeRegistry(), config=SimpleNamespace(auth_enabled=False), add_to_favorites=None, force=False)

    assert calls and calls[0]["user_id"] is None
    assert session.get(Download, result["download_id"]).requested_by is None


def test_retry_missing_track_404s(session, monkeypatch):
    monkeypatch.setattr("services.downloads.queue_download", lambda **kw: None)
    with pytest.raises(HTTPException) as exc:
        retry_download("does-not-exist", session, user=Downloader(_SYSTEM), registry=FakeRegistry(), config=SimpleNamespace(auth_enabled=False), add_to_favorites=None, force=False)
    assert exc.value.status_code == 404


# ------------------------------------------------------------------
# GET /downloads — un follower (aggancio a una richiesta altrui, vedi
# attach_to_open_download) la vede senza che requested_by cambi.
# ------------------------------------------------------------------

def test_get_downloads_follower_sees_the_download_they_attached_to(session, registry):
    owner = _make_user(session, role="user", username="owner")
    follower = _make_user(session, role="user", username="follower")
    track = TrackRecord(title="Song", artist="Artist", album="Album")
    session.add(track)
    session.commit()
    session.refresh(track)
    dl = Download(track_id=track.internal_id, status="downloading", requested_by=owner.id)
    session.add(dl)
    session.commit()
    session.refresh(dl)

    add_follower(session, dl.id, follower.id)

    result = get_downloads(session, registry, auth_manager=object(), viewer=GUEST, user=follower, page=1, limit=50, status=None, q=None, sort="recent")

    assert result["total"] == 1
    item = result["items"][0]
    assert item["track_id"] == track.internal_id
    # requested_by resta il richiedente originale, non il follower
    assert item["requested_by"] == "owner"
    assert len(item["attempts"]) == 1


def test_get_downloads_a_third_user_does_not_see_a_download_they_are_not_attached_to(session, registry):
    owner = _make_user(session, role="user", username="owner")
    follower = _make_user(session, role="user", username="follower")
    stranger = _make_user(session, role="user", username="stranger")
    track = TrackRecord(title="Song", artist="Artist", album="Album")
    session.add(track)
    session.commit()
    session.refresh(track)
    dl = Download(track_id=track.internal_id, status="downloading", requested_by=owner.id)
    session.add(dl)
    session.commit()
    session.refresh(dl)

    add_follower(session, dl.id, follower.id)

    result = get_downloads(session, registry, auth_manager=object(), viewer=GUEST, user=stranger, page=1, limit=50, status=None, q=None, sort="recent")

    assert result["total"] == 0
    assert result["items"] == []


def test_get_downloads_follower_does_not_see_unrelated_attempts_on_the_same_track(session, registry):
    # Stesso vincolo di prima (nessuna leak di attempt altrui), ma con un
    # follower invece del solo requester.
    owner = _make_user(session, role="user", username="owner")
    follower = _make_user(session, role="user", username="follower")
    stranger = _make_user(session, role="user", username="stranger")
    track_id = _make_track_with_downloads(
        session, requested_by_pairs=[("downloading", owner.id), ("failed", stranger.id)]
    )
    owner_dl = session.exec(
        Download.__table__.select().where(Download.requested_by == owner.id)
    ).first()
    add_follower(session, owner_dl.id, follower.id)

    result = get_downloads(session, registry, auth_manager=object(), viewer=GUEST, user=follower, page=1, limit=50, status=None, q=None, sort="recent")

    assert result["total"] == 1
    item = result["items"][0]
    assert item["track_id"] == track_id
    assert len(item["attempts"]) == 1
    assert item["attempts"][0]["download_id"] == owner_dl.id
