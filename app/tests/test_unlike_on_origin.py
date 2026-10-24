"""
Taking a like back on the provider it came from is each user's own choice.

``unlike_on_origin_providers`` is a personal setting (scope ``user``, off by
default): who may see and write it is the declaration, never a role check.
When a user takes a favorite off, the removal reads THAT user's setting and,
if it is on, the task takes the imported like back only with THAT user's own
connected account: never another user's, never the shared system account,
never an app-only token. A connection made before the provider asked for the
permission is reported (``missing_permissions`` in GET /providers, the
``missing_permission`` outcome of the task) instead of failing silently.
"""
from __future__ import annotations

import logging
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from core.auth import AccountTarget, missing_capabilities
from musizeer_sdk.auth import (
    AccountModel,
    AuthDefinition,
    AuthFlow,
    BearerHeaderRequestAuth,
    ClientCredentialsFallback,
    OAuth2Config,
)
from core.auth_manager import AuthManager
from musizeer_sdk.capabilities import CAPABILITY_UNLIKE_SAVED_TRACK, CAPABILITY_USER_LIBRARY
from core.settings_catalog import get_def
from core.user_auth import create_access_token
from db.auth_store import CredentialStore
from db.engine import get_session
from db.models import Setting, User, UserSavedTrack, UserSavedTrackProvider
from routes import config as config_routes
from routes import deps
from routes.config import router as config_router
from services import favorites
from services.config_manager import ConfigManager
from services.settings import personal_value

from tests.conftest import FakeMetadataProvider, FakeRegistry
from tests.test_favorite_removal import _library_track

KEY = "unlike_on_origin_providers"
TASK = "workers.maintenance.unlike_on_origin_providers"
READ, MODIFY = "library-read", "library-modify"


def _user(engine, name: str, role: str = "user") -> str:
    with Session(engine) as session:
        user = User(username=name, password_hash="x", role=role)
        session.add(user)
        session.commit()
        return user.id


def _choose(engine, user_id: str, value: bool) -> None:
    from db.settings_store import SettingsRepository

    with Session(engine) as session:
        SettingsRepository(session).set_user(KEY, user_id, value)


class _AppTokenUse:
    """How many times the app-only token (what a provider falls back to without a
    user token) was asked for, so a test can prove the unlike never took it."""

    def __init__(self) -> None:
        self.used = 0


@pytest.fixture(autouse=True)
def _app_only_token(monkeypatch):
    def fetch_client_credentials_token(self, provider):
        provider.fallback.used += 1
        return {"access_token": "app-token"}

    monkeypatch.setattr(AuthManager, "fetch_client_credentials_token", fetch_client_credentials_token)


class _Origin(FakeMetadataProvider):
    """An OAuth2 metadata provider whose API may let an app take a like off.
    Records the access token each unlike was sent with."""

    def __init__(self, name: str = "origin", *, supports: bool = True, refuses: bool = False) -> None:
        super().__init__(name)
        self.fallback = _AppTokenUse()
        self.auth = AuthDefinition(flow=AuthFlow.OAUTH2, account_model=AccountModel.PERSONAL_WITH_SHARED, oauth2=OAuth2Config(
            authorize_url=f"https://{name}.test/authorize", token_url=f"https://{name}.test/token",
            scopes=[READ], request_auth=BearerHeaderRequestAuth(), fallback_auth=ClientCredentialsFallback(),
            capability_scopes={CAPABILITY_UNLIKE_SAVED_TRACK: [MODIFY]} if supports else {},
        ))
        self.supports, self.refuses = supports, refuses
        self.unliked: list[tuple[str, str]] = []

    def supports_user_library(self):
        return True

    def supports_unlike_saved_track(self):
        return self.supports

    def unlike_saved_track(self, provider_id, session):
        if self.refuses:
            raise PermissionError("the account did not grant library-modify: reconnect it")
        self.unliked.append((provider_id, session.headers["Authorization"]))


def _token(access_token: str, *, scope: str | None = f"{READ} {MODIFY}", **extra) -> dict:
    data = {"access_token": access_token, "expires_in": 3600, "obtained_at": time.time(), **extra}
    if scope is not None:
        data["scope"] = scope
    return data


def _connect(engine, user_id: str | None, provider: str, token: dict) -> None:
    """``user_id`` None: the shared system account."""
    with Session(engine) as session:
        CredentialStore(session, user_id=user_id).save_oauth2_token(
            provider, token, AccountTarget.SHARED if user_id is None else AccountTarget.PERSONAL)


def _liked_on(engine, user_id: str, track_id: str, provider: str, provider_track_id: str) -> None:
    with Session(engine) as session:
        saved = session.exec(select(UserSavedTrack).where(
            UserSavedTrack.user_id == user_id, UserSavedTrack.track_id == track_id)).first()
        if saved is None:
            saved = UserSavedTrack(user_id=user_id, track_id=track_id, library_track_id=track_id)
            session.add(saved)
            session.flush()
        session.add(UserSavedTrackProvider(saved_track_id=saved.id, provider=provider, provider_track_id=provider_track_id))
        session.commit()


def _links(engine, user_id: str) -> list[str]:
    with Session(engine) as session:
        return [link.provider for _saved, link in session.exec(
            select(UserSavedTrack, UserSavedTrackProvider)
            .join(UserSavedTrackProvider, UserSavedTrackProvider.saved_track_id == UserSavedTrack.id)
            .where(UserSavedTrack.user_id == user_id)
        ).all()]


def _library(engine) -> str:
    with Session(engine) as session:
        return _library_track(session)


@pytest.fixture
def run_unlike(engine, monkeypatch):
    from workers import maintenance

    def run(user_id: str, track_id: str, *providers):
        registry = FakeRegistry(metadata=list(providers))
        monkeypatch.setattr(maintenance, "engine", engine)
        monkeypatch.setattr(maintenance, "get_registry", lambda: registry)
        monkeypatch.setattr(maintenance, "auth_manager_for", lambda store: AuthManager(store, token_expiry_buffer=0))
        return maintenance.unlike_on_origin_providers.run(user_id, track_id)

    return run


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def test_it_is_a_personal_setting_off_by_default_in_the_account_area():
    setting = get_def(KEY)

    assert (setting.scope, setting.default, setting.type, setting.area) == ("user", False, "bool", "account")
    assert setting.provider_capability == CAPABILITY_UNLIKE_SAVED_TRACK
    public = setting.to_public()
    assert public["provider_capability"] == CAPABILITY_UNLIKE_SAVED_TRACK
    assert (public["label_key"], public["description_key"]) == (f"settings.labels.{KEY}", f"settings.descriptions.{KEY}")


def test_the_value_of_one_user_is_their_own_and_the_no_login_user_has_none(engine):
    alice, bob = _user(engine, "alice"), _user(engine, "bob")
    _choose(engine, alice, True)

    with Session(engine) as session:
        assert personal_value(session, alice, KEY) is True
        assert personal_value(session, bob, KEY) is False
        assert personal_value(session, "__system__", KEY) is False
        with pytest.raises(ValueError):
            personal_value(session, alice, "auth_enabled")


# ---------------------------------------------------------------------------
# Who sees and writes it, over HTTP: guest, user, admin, open mode
# ---------------------------------------------------------------------------


@pytest.fixture
def client_for(engine, monkeypatch):
    monkeypatch.setattr(config_routes, "auth_manager_for", lambda store: AuthManager(store, token_expiry_buffer=0))

    def build(*, auth_enabled: bool = True, providers=()) -> TestClient:
        config = ConfigManager(engine=engine)
        config.update({"auth_enabled": auth_enabled})
        app = FastAPI()
        app.include_router(config_router)

        def _session():
            with Session(engine) as session:
                yield session

        registry = FakeRegistry(metadata=list(providers))
        app.dependency_overrides[get_session] = _session
        app.dependency_overrides[deps.get_config] = lambda: config
        app.dependency_overrides[deps.get_registry] = lambda: registry
        return TestClient(app)

    return build


def _as(engine, name: str, role: str = "user") -> tuple[str, dict]:
    user_id = _user(engine, name, role)
    return user_id, {"Authorization": f"Bearer {create_access_token(user_id, role)}"}


def _schema_entry(client, headers) -> dict | None:
    return next((s for s in client.get("/settings/schema", headers=headers).json()["settings"] if s["key"] == KEY), None)


def _rows(engine) -> list[tuple[str, str | None, str]]:
    with Session(engine) as session:
        return sorted((row.scope, row.user_id, row.value) for row in session.exec(select(Setting).where(Setting.key == KEY)))


def test_a_guest_has_no_settings_at_all(engine, client_for):
    client = client_for()

    assert client.get("/settings/schema").status_code == 401
    assert client.get("/settings/values").status_code == 401
    assert client.patch("/settings", json={KEY: True}).status_code == 401
    assert _rows(engine) == []


@pytest.mark.parametrize("role", ["user", "admin"])
def test_a_signed_in_user_or_admin_sees_it_and_writes_only_their_own_row(engine, client_for, role):
    client = client_for()
    me, headers = _as(engine, "me", role)
    other, other_headers = _as(engine, "other")

    entry = _schema_entry(client, headers)
    assert entry["scope"] == "user" and entry["area"] == "account" and entry["type"] == "bool"
    # Never chosen: the key is absent (the UI shows it off), never a system value.
    assert KEY not in client.get("/settings/values", headers=headers).json()

    assert client.patch("/settings", json={KEY: True}, headers=headers).json()[KEY] is True

    assert _rows(engine) == [("user", me, "true")]
    assert KEY not in client.get("/settings/values", headers=other_headers).json()


def test_two_users_keep_two_different_choices(engine, client_for):
    client = client_for()
    alice, alice_headers = _as(engine, "alice")
    bob, bob_headers = _as(engine, "bob")

    client.patch("/settings", json={KEY: True}, headers=alice_headers)
    client.patch("/settings", json={KEY: False}, headers=bob_headers)

    assert client.get("/settings/values", headers=alice_headers).json()[KEY] is True
    assert client.get("/settings/values", headers=bob_headers).json()[KEY] is False
    assert _rows(engine) == sorted([("user", alice, "true"), ("user", bob, "false")])


def test_in_open_mode_the_owner_chooses_it_for_the_instance_user(engine, client_for):
    from db.models import SYSTEM_USER_ID
    from tests.database import seed_system_user

    seed_system_user(engine)  # what the application creates at start
    client = client_for(auth_enabled=False)

    assert KEY not in client.get("/settings/values").json()  # never chosen: omitted
    assert client.patch("/settings", json={KEY: True}).status_code == 200
    assert client.get("/settings/values").json()[KEY] is True
    assert _rows(engine) == [("user", SYSTEM_USER_ID, "true")]


# ---------------------------------------------------------------------------
# The removal reads the setting of the user who removes the favorite
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatched(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(favorites, "dispatch", lambda name, **kwargs: sent.append((name, kwargs)))
    monkeypatch.setattr(favorites, "get_config", lambda: type("Config", (), {"sync_provider": ""})())
    return sent


def test_the_removal_asks_the_task_only_for_the_user_who_chose_it(engine, dispatched):
    from tests.test_favorite_removal import REGISTRY

    alice, bob = _user(engine, "alice"), _user(engine, "bob")
    _choose(engine, alice, True)
    _choose(engine, bob, False)
    library = _library(engine)
    with Session(engine) as session:
        for user in (alice, bob):
            favorites.add(session, user, library, REGISTRY)
            favorites.remove(session, user, library)

    assert [kwargs for name, kwargs in dispatched if name == TASK] == [{"user_id": alice, "track_id": library}]


def test_the_task_does_nothing_for_a_user_who_did_not_choose_it(engine, run_unlike):
    alice = _user(engine, "alice")
    library = _library(engine)
    origin = _Origin()
    _connect(engine, alice, "origin", _token("token-alice"))
    _liked_on(engine, alice, library, "origin", "o-1")

    assert run_unlike(alice, library, origin) == {"status": "disabled"}
    _choose(engine, alice, False)
    assert run_unlike(alice, library, origin) == {"status": "disabled"}

    assert origin.unliked == [] and _links(engine, alice) == ["origin"]


# ---------------------------------------------------------------------------
# Only the remover's own account, never another user's or the system's
# ---------------------------------------------------------------------------


def test_the_like_is_taken_back_with_the_removers_own_token_only(engine, run_unlike):
    alice, bob = _user(engine, "alice"), _user(engine, "bob")
    _choose(engine, alice, True)
    _choose(engine, bob, True)
    library = _library(engine)
    origin = _Origin()
    _connect(engine, None, "origin", _token("token-system"))
    _connect(engine, alice, "origin", _token("token-alice"))
    _connect(engine, bob, "origin", _token("token-bob"))
    _liked_on(engine, alice, library, "origin", "o-alice")
    _liked_on(engine, bob, library, "origin", "o-bob")

    result = run_unlike(alice, library, origin)

    assert origin.unliked == [("o-alice", "Bearer token-alice")]
    assert result["unliked"] == 1
    assert result["providers"] == [{"provider": "origin", "outcome": "unliked", "reason": None}]
    # Alice's imported like is forgotten; Bob's like and account are untouched.
    assert _links(engine, alice) == [] and _links(engine, bob) == ["origin"]
    assert origin.fallback.used == 0


@pytest.mark.parametrize("role", ["user", "admin"])
def test_without_an_own_account_the_system_account_is_never_used(engine, run_unlike, role):
    me = _user(engine, "me", role)
    _choose(engine, me, True)
    library = _library(engine)
    origin = _Origin()
    _connect(engine, None, "origin", _token("token-system"))
    _liked_on(engine, me, library, "origin", "o-1")

    result = run_unlike(me, library, origin)

    assert origin.unliked == [] and origin.fallback.used == 0
    assert result["no_personal_account"] == 1
    assert result["providers"][0]["outcome"] == "no_personal_account"
    assert _links(engine, me) == ["origin"]


def test_in_open_mode_the_owner_takes_the_like_back_with_the_connected_account(engine, run_unlike):
    from db.models import SYSTEM_USER_ID
    from tests.database import seed_system_user

    seed_system_user(engine)
    _choose(engine, SYSTEM_USER_ID, True)
    library = _library(engine)
    origin = _Origin()
    _connect(engine, None, "origin", _token("token-instance"))  # connected from Administration > Providers
    _liked_on(engine, SYSTEM_USER_ID, library, "origin", "o-1")

    result = run_unlike(SYSTEM_USER_ID, library, origin)

    assert origin.unliked == [("o-1", "Bearer token-instance")]
    assert result["unliked"] == 1 and _links(engine, SYSTEM_USER_ID) == []
    assert origin.fallback.used == 0


def test_with_authentication_on_the_system_user_has_no_account_to_take_a_like_back_with(engine, run_unlike):
    from db.models import SYSTEM_USER_ID
    from tests.database import seed_system_user

    seed_system_user(engine)
    ConfigManager(engine=engine).update({"auth_enabled": True})
    _choose(engine, SYSTEM_USER_ID, True)
    library = _library(engine)
    origin = _Origin()
    _connect(engine, None, "origin", _token("token-instance"))
    _liked_on(engine, SYSTEM_USER_ID, library, "origin", "o-1")

    result = run_unlike(SYSTEM_USER_ID, library, origin)

    assert origin.unliked == [] and result["no_personal_account"] == 1


def test_a_disconnected_account_is_no_account(engine, run_unlike):
    alice = _user(engine, "alice")
    _choose(engine, alice, True)
    library = _library(engine)
    origin = _Origin()
    _connect(engine, None, "origin", _token("token-system"))
    _connect(engine, alice, "origin", _token("token-alice"))
    with Session(engine) as session:
        CredentialStore(session, user_id=alice).delete_oauth2_token("origin", AccountTarget.PERSONAL)
    _liked_on(engine, alice, library, "origin", "o-1")

    assert run_unlike(alice, library, origin)["no_personal_account"] == 1
    assert origin.unliked == [] and origin.fallback.used == 0


def test_an_expired_own_token_that_cannot_refresh_never_falls_back_to_an_app_token(engine, run_unlike):
    alice = _user(engine, "alice")
    _choose(engine, alice, True)
    library = _library(engine)
    origin = _Origin()
    _connect(engine, alice, "origin", _token("stale", obtained_at=0, refresh_token="r"))
    _liked_on(engine, alice, library, "origin", "o-1")

    result = run_unlike(alice, library, origin)

    assert result["failed"] == 1 and result["providers"][0]["reason"]
    assert origin.unliked == [] and origin.fallback.used == 0
    assert _links(engine, alice) == ["origin"]


def test_a_provider_without_support_is_skipped(engine, run_unlike):
    alice = _user(engine, "alice")
    _choose(engine, alice, True)
    library = _library(engine)
    closed = _Origin("closed", supports=False)
    _connect(engine, alice, "closed", _token("token-alice"))
    _liked_on(engine, alice, library, "closed", "c-1")
    _liked_on(engine, alice, library, "gone", "g-1")  # no longer installed

    result = run_unlike(alice, library, closed)

    assert closed.unliked == []
    assert (result["not_supported"], result["unliked"]) == (2, 0)
    assert sorted(_links(engine, alice)) == ["closed", "gone"]


# ---------------------------------------------------------------------------
# A connection made before the permission was asked
# ---------------------------------------------------------------------------


def test_an_account_connected_without_the_permission_is_reported_not_called(engine, run_unlike, caplog):
    alice = _user(engine, "alice")
    _choose(engine, alice, True)
    library = _library(engine)
    origin = _Origin()
    _connect(engine, alice, "origin", _token("token-alice", scope=READ))
    _liked_on(engine, alice, library, "origin", "o-1")

    with caplog.at_level(logging.WARNING, logger="workers.maintenance"):
        result = run_unlike(alice, library, origin)

    assert origin.unliked == []
    assert result["missing_permission"] == 1
    assert "reconnect" in result["providers"][0]["reason"]
    assert _links(engine, alice) == ["origin"]
    assert any(alice in record.getMessage() and "missing_permission" in record.getMessage() for record in caplog.records)


def test_a_refusal_of_the_provider_is_a_missing_permission_with_its_reason(engine, run_unlike):
    alice = _user(engine, "alice")
    _choose(engine, alice, True)
    library = _library(engine)
    origin = _Origin(refuses=True)
    # The token does not list its scopes: only the call can tell.
    _connect(engine, alice, "origin", _token("token-alice", scope=None))
    _liked_on(engine, alice, library, "origin", "o-1")

    result = run_unlike(alice, library, origin)

    assert result["missing_permission"] == 1
    assert result["providers"][0] == {
        "provider": "origin", "outcome": "missing_permission",
        "reason": "the account did not grant library-modify: reconnect it",
    }
    assert _links(engine, alice) == ["origin"]


def test_missing_capabilities_compares_the_granted_scopes():
    config = OAuth2Config(authorize_url="a", token_url="t", scopes=[READ],
                          capability_scopes={CAPABILITY_UNLIKE_SAVED_TRACK: [MODIFY]})

    assert missing_capabilities(config, {"scope": READ}) == [CAPABILITY_UNLIKE_SAVED_TRACK]
    assert missing_capabilities(config, {"scope": f"{MODIFY} {READ}"}) == []
    assert missing_capabilities(config, {"access_token": "x"}) == []  # unknown: not reported
    assert missing_capabilities(None, {"scope": READ}) == []
    # A new connection asks for every capability scope.
    assert config.requested_scopes() == [READ, MODIFY]


def test_providers_shows_the_viewers_own_missing_permission(engine, client_for):
    origin = _Origin()
    client = client_for(providers=[origin])
    old, old_headers = _as(engine, "old")
    new, new_headers = _as(engine, "new")
    _, nobody_headers = _as(engine, "nobody")
    _connect(engine, None, "origin", _token("token-system", scope=READ))
    _connect(engine, old, "origin", _token("token-old", scope=READ))
    _connect(engine, new, "origin", _token("token-new"))

    def info(headers):
        return next(p for p in client.get("/providers", headers=headers).json()["metadata"] if p["name"] == "origin")

    assert info(old_headers)["capabilities"] == [CAPABILITY_USER_LIBRARY, CAPABILITY_UNLIKE_SAVED_TRACK]
    assert info(old_headers)["personal"]["missing_permissions"] == [CAPABILITY_UNLIKE_SAVED_TRACK]
    assert info(new_headers)["personal"]["missing_permissions"] == []
    # Only the shared account: nothing of theirs to reconnect.
    assert info(nobody_headers)["personal"] == {
        "connected": False, "username": None, "missing_permissions": [], "capabilities": [CAPABILITY_USER_LIBRARY, CAPABILITY_UNLIKE_SAVED_TRACK],
        "login": None,
    }
    assert info(nobody_headers)["shared"]["connected"] is True

