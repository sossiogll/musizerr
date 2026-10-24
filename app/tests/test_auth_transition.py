"""
The switch between the two modes (``services/auth_transition.py``, docs/permissions.md):

- off to on: the open-mode owner's data becomes an administrator's in the SAME transaction as the
  switch; a failure half way leaves everything as it was;
- the administrator is the one asked for (``adopt_into``) or the oldest that can sign in;
- on to off: nothing moves back; open mode starts again from what the synthetic user holds.

The rules of what moves are tested on the rows themselves in ``test_adoption.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from core.auth import AccountTarget
from db import adoption
from db.models import SYSTEM_USER_ID, Playlist, ProviderCredential, Setting, UserFavorite
from db.settings_store import SettingsRepository
from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow, BearerHeaderRequestAuth, OAuth2Config
from services import auth_transition
from services.credentials import account_holders, open_mode, own_store, user_store
from services.settings import SettingsService, SettingsWriteError

from tests.auth_modes_support import (  # noqa: F401
    count_owned,
    owned_by,
    owner_data,
    sessions_of_the_synthetic_user,
    world,
)
from tests.conftest import FakeMetadataProvider, FakeRegistry
from tests.test_openapi_responses import assert_conforms

LONG_AGO = datetime.now(timezone.utc) - timedelta(days=30)


def _turn_on(world, **query):
    return world.client.patch("/settings", json={"auth_enabled": True}, params=query)


def _favorites(world, user_id: str) -> list[str]:
    with world.session() as session:
        return sorted(f.track_id for f in owned_by(session, UserFavorite, user_id))


def _owner_with_data(world, *, admins: int = 1):
    """An open instance whose owner has data, and ``admins`` administrators (the first the oldest)."""
    world.set_authentication(False)
    with world.session() as session:
        ids = owner_data(session, SYSTEM_USER_ID, tag="owner")
    accounts = [
        world.account(f"admin-{n}", role="admin", created_at=LONG_AGO + timedelta(days=n)) for n in range(1, admins + 1)
    ]
    return ids, accounts


# ---------------------------------------------------------------------------
# Off to on
# ---------------------------------------------------------------------------


def test_turning_authentication_on_gives_the_owners_data_to_the_oldest_administrator(world):
    _, (first, second) = _owner_with_data(world, admins=2)

    done = _turn_on(world)

    assert done.status_code == 200, done.text
    assert world.config.auth_enabled is True
    assert _favorites(world, first.id) == ["owner-t1"]
    assert _favorites(world, second.id) == [] and _favorites(world, SYSTEM_USER_ID) == []
    with world.session() as session:
        assert len(owned_by(session, Playlist, first.id)) == 2
        assert [s.value for s in owned_by(session, Setting, first.id)] == ['"owner-theme"']


def test_the_administrator_asked_for_takes_the_data(world):
    _, (first, second) = _owner_with_data(world, admins=2)

    done = _turn_on(world, adopt_into=second.id)

    assert done.status_code == 200, done.text
    assert _favorites(world, second.id) == ["owner-t1"] and _favorites(world, first.id) == []


@pytest.mark.parametrize("target", [
    pytest.param("nobody", id="unknown"),
    pytest.param(SYSTEM_USER_ID, id="the-synthetic-user"),
    pytest.param("plain", id="a-plain-user"),
    pytest.param("inactive", id="an-inactive-administrator"),
])
def test_data_cannot_go_to_an_account_that_cannot_sign_in_as_an_administrator(world, target):
    _owner_with_data(world)
    plain, inactive = world.account("plain"), world.account("inactive", role="admin", active=False)
    chosen = {"plain": plain.id, "inactive": inactive.id}.get(target, target)

    refused = _turn_on(world, adopt_into=chosen)

    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"]["error_code"] == "adoption_target_invalid"
    assert_conforms(refused, "PATCH", "/settings")
    assert world.config.auth_enabled is False
    assert _favorites(world, SYSTEM_USER_ID) == ["owner-t1"]  # nothing moved


def test_the_switch_and_the_rest_of_the_batch_are_applied_together(world):
    _, (admin,) = _owner_with_data(world)

    done = world.client.patch("/settings", json={"auth_enabled": True, "save_cover": False})

    assert done.status_code == 200 and done.json()["save_cover"] is False
    assert world.config.auth_enabled is True and world.config.get("save_cover") is False
    assert _favorites(world, admin.id) == ["owner-t1"]


def test_a_batch_refused_for_another_key_moves_nothing(world):
    _owner_with_data(world)

    refused = world.client.patch("/settings", json={"auth_enabled": True, "no_such_setting": 1})

    assert refused.status_code == 400
    assert world.config.auth_enabled is False and _favorites(world, SYSTEM_USER_ID) == ["owner-t1"]


def test_the_owners_sessions_and_policies_are_not_adopted(world):
    _owner_with_data(world)
    with world.session() as session:
        sessions_of_the_synthetic_user(session)

    assert _turn_on(world).status_code == 200

    with world.session() as session:
        for table, column in adoption.NOT_ADOPTED:
            if table != "provider_credentials":
                assert count_owned(session, table, column, SYSTEM_USER_ID) == 1, table


# ---------------------------------------------------------------------------
# One transaction
# ---------------------------------------------------------------------------


def _write(world, **values):
    admin = world.account("writer", role="admin")  # the caller: any administrator
    with world.session() as session:
        return SettingsService(session, world.config, world.registry).write_values(values, admin)


def _everything_stays_as_before(world):
    assert world.config.auth_enabled is False
    with world.session() as session:
        assert SettingsRepository(session).get_system("auth_enabled", False) is False
        assert open_mode(session)
        assert len(owned_by(session, UserFavorite, SYSTEM_USER_ID)) == 1
        assert len(owned_by(session, Playlist, SYSTEM_USER_ID)) == 2
        assert len(owned_by(session, Setting, SYSTEM_USER_ID)) == 1


def test_a_failure_while_moving_the_data_leaves_everything_as_before(world, monkeypatch):
    _owner_with_data(world)

    def boom(*args, **kwargs):
        raise RuntimeError("the database went away")

    # After the other tables were moved.
    monkeypatch.setattr(adoption, "adopt_personal_accounts", boom)

    with pytest.raises(RuntimeError):
        _write(world, auth_enabled=True)

    _everything_stays_as_before(world)


def test_a_failure_writing_the_switch_after_the_data_moved_undoes_the_move(world, monkeypatch):
    _owner_with_data(world)
    real = SettingsRepository.set_system

    def failing(self, key, value, **kwargs):
        real(self, key, value, **kwargs)  # the write itself happened, uncommitted...
        raise RuntimeError("...and the commit never came")

    monkeypatch.setattr(SettingsRepository, "set_system", failing)

    with pytest.raises(RuntimeError):
        _write(world, auth_enabled=True)

    _everything_stays_as_before(world)


def test_a_commit_that_fails_leaves_everything_as_before(world, monkeypatch):
    _, (admin,) = _owner_with_data(world)

    def refused():
        raise RuntimeError("commit refused")

    with world.session() as session:
        monkeypatch.setattr(session, "commit", refused)
        with pytest.raises(RuntimeError):
            auth_transition.turn_on(session, world.config, world.registry, administrator_id=admin.id)

    _everything_stays_as_before(world)


def test_the_switch_is_a_no_op_when_authentication_is_already_on(world):
    _, (admin,) = _owner_with_data(world)
    assert _turn_on(world).status_code == 200
    with world.session() as session:  # the synthetic user holds something new, written meanwhile
        session.add(Setting(key="language", scope="user", user_id=SYSTEM_USER_ID, value='"it"'))
        session.commit()

    again = world.client.patch("/settings", json={"auth_enabled": True}, headers=world.headers(admin))

    assert again.status_code == 200
    with world.session() as session:
        assert [s.key for s in owned_by(session, Setting, SYSTEM_USER_ID)] == ["language"]  # not adopted a second time


# ---------------------------------------------------------------------------
# The accounts
# ---------------------------------------------------------------------------


def _provider(name: str, model: AccountModel):
    provider = FakeMetadataProvider(name)
    provider.auth = AuthDefinition(
        flow=AuthFlow.OAUTH2, account_model=model,
        oauth2=OAuth2Config(
            authorize_url="https://example.invalid/authorize", token_url="https://example.invalid/token",
            scopes=[], request_auth=BearerHeaderRequestAuth(),
        ),
    )
    return provider


@pytest.fixture
def accounts_world(world):
    world.registry = FakeRegistry(metadata=[
        _provider("likes", AccountModel.PERSONAL),
        _provider("mixed", AccountModel.PERSONAL_WITH_SHARED),
        _provider("shared", AccountModel.SYSTEM),
    ])
    return world


def _connect_as_the_owner(world):
    """In open mode the owner's own account IS the shared one: whatever they connect is stored shared."""
    token = {"access_token": "t", "obtained_at": 1.0, "expires_in": 10**9}
    with world.session() as session:
        store = user_store(session, SYSTEM_USER_ID)
        assert store.instance_owner
        for name in ("likes", "mixed"):
            store.save_oauth2_token(name, token, AccountTarget.PERSONAL)
        from db.auth_store import CredentialStore

        CredentialStore(session).save_oauth2_token("shared", token, AccountTarget.SHARED)
        session.commit()


def test_the_owners_personal_only_account_follows_them_and_the_shared_accounts_stay_shared(accounts_world):
    world = accounts_world
    _, (admin,) = _owner_with_data(world)
    _connect_as_the_owner(world)

    assert _turn_on(world).status_code == 200

    with world.session() as session:
        rows = {(c.provider, c.user_id) for c in session.exec(select(ProviderCredential)).all()}
        assert rows == {("likes", admin.id), ("mixed", None), ("shared", None)}
        # The administrator's own calls work with the account they inherited, and the others read the shared ones.
        assert own_store(session, admin.id).own_oauth2_token("likes") is not None
        assert own_store(session, admin.id).own_oauth2_token("mixed") is None  # no personal copy of a shared account
        assert user_store(session, admin.id).shared_oauth2_token("mixed") is not None  # but the shared one serves them
        assert user_store(session, SYSTEM_USER_ID).own_oauth2_token("likes") is None  # the synthetic user is nobody now


# ---------------------------------------------------------------------------
# On to off, and back
# ---------------------------------------------------------------------------


def test_turning_authentication_off_moves_nothing_back(world):
    _, (admin,) = _owner_with_data(world)
    assert _turn_on(world).status_code == 200
    with world.session() as session:
        before_admin = sorted(f.id for f in owned_by(session, UserFavorite, admin.id))

    off = world.client.patch("/settings", json={"auth_enabled": False}, headers=world.headers(admin))

    assert off.status_code == 200, off.text
    assert world.config.auth_enabled is False
    with world.session() as session:
        # The administrator's data stays the administrator's; the synthetic user starts from what it still holds.
        assert sorted(f.id for f in owned_by(session, UserFavorite, admin.id)) == before_admin
        assert owned_by(session, UserFavorite, SYSTEM_USER_ID) == []
        assert open_mode(session)
        assert [u.id for u in account_holders(session)] == [SYSTEM_USER_ID]


def test_only_an_administrator_can_turn_it_off(world):
    _, (admin,) = _owner_with_data(world)
    assert _turn_on(world).status_code == 200
    plain = world.account("plain")

    assert world.client.patch("/settings", json={"auth_enabled": False}).status_code == 401
    assert world.client.patch("/settings", json={"auth_enabled": False}, headers=world.headers(plain)).status_code == 403
    assert world.config.auth_enabled is True


def test_a_round_trip_keeps_each_stints_data_with_its_owner(world):
    _, (admin,) = _owner_with_data(world)
    assert _turn_on(world).status_code == 200
    assert world.client.patch("/settings", json={"auth_enabled": False}, headers=world.headers(admin)).status_code == 200

    # A second stint in open mode: the owner favorites something new...
    with world.session() as session:
        from tests.database import seed_parents

        seed_parents(session, tracks=["second-stint"])
        session.add(UserFavorite(user_id=SYSTEM_USER_ID, track_id="second-stint"))
        session.commit()
    # ...and authentication goes on again: it joins the administrator's.
    assert _turn_on(world).status_code == 200

    assert _favorites(world, admin.id) == ["owner-t1", "second-stint"]
    assert _favorites(world, SYSTEM_USER_ID) == []


def test_the_service_raises_a_coded_error_the_routes_pass_on(world):
    world.set_authentication(False)
    with world.session() as session, pytest.raises(auth_transition.TransitionRefused) as refused:
        auth_transition.turn_on(session, world.config, world.registry)

    assert refused.value.error_code == "no_admin_account"
    assert world.config.auth_enabled is False


def test_an_administrator_who_disappears_after_the_check_is_caught_again_under_the_lock(world, monkeypatch):
    """The guard passed, then the only administrator was deactivated: the transition asks again."""
    _, (admin,) = _owner_with_data(world)
    monkeypatch.setattr(auth_transition.user_service, "administrators", lambda session: [])

    with world.session() as session, pytest.raises(auth_transition.TransitionRefused) as refused:
        auth_transition.turn_on(session, world.config, world.registry)

    assert refused.value.error_code == "no_admin_account"
    _everything_stays_as_before(world)
