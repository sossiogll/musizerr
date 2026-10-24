"""
The two authentication modes, seen from the API:

- the status says the same thing the register and login routes do (with authentication off nobody
  can register, and the status no longer says they can);
- Administration › Users creates accounts, with authentication on AND off (``POST /admin/users``);
- authentication cannot be turned on while nobody can sign in (``no_admin_account``), on top of the
  ``MUSIZEER_AUTH_SECRET`` rule, in the same ``guard_on`` every writer of the setting passes through.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core import user_auth
from db import user_service
from db.models import SYSTEM_USER_ID, User

from tests.auth_modes_support import LONG_ENOUGH, world  # noqa: F401
from tests.test_openapi_responses import assert_conforms

NEW_USER = {"username": "alice", "password": LONG_ENOUGH}


# ---------------------------------------------------------------------------
# The status, per mode
# ---------------------------------------------------------------------------


def test_with_authentication_off_nobody_can_register_and_the_status_says_so(world):
    world.set_authentication(False, registration_open=True)  # the setting is on; it means nothing here

    for path in ("/auth/account/status", "/auth/status"):
        body = world.client.get(path).json()
        assert body["auth_enabled"] is False and body["registration_open"] is False, path
    assert world.client.get("/auth/account/status").json()["mode"] == "open"
    # What the status promises is what the routes answer.
    assert world.client.post("/auth/account/register", json=NEW_USER).status_code == 400
    assert world.client.post("/auth/account/login", json=NEW_USER).status_code == 400


@pytest.mark.parametrize("setting", [True, False])
def test_with_authentication_on_the_status_follows_the_registration_setting(world, setting):
    world.set_authentication(True, registration_open=setting)

    for path in ("/auth/account/status", "/auth/status"):
        assert world.client.get(path).json()["registration_open"] is setting, path
    assert world.client.get("/auth/account/status").json()["mode"] == "guest"


def test_the_status_of_an_open_instance_conforms_to_the_documented_one(world):
    world.set_authentication(False)

    assert_conforms(world.client.get("/auth/account/status"), "GET", "/auth/account/status")


def test_with_authentication_on_registration_and_login_work_as_they_did(world):
    world.set_authentication(True, registration_open=True)

    registered = world.client.post("/auth/account/register", json=NEW_USER)
    assert registered.status_code == 201 and registered.json()["role"] == "user"
    assert world.client.post("/auth/account/register", json=NEW_USER).status_code == 409
    assert world.client.post("/auth/account/login", json=NEW_USER).status_code == 200
    assert world.client.post("/auth/account/login", json={**NEW_USER, "password": "wrong-password"}).status_code == 401
    world.set_authentication(True, registration_open=False)
    assert world.client.post("/auth/account/register", json={**NEW_USER, "username": "bob"}).status_code == 403


def test_a_registration_cannot_take_the_name_of_the_synthetic_user(world):
    world.set_authentication(True, registration_open=True)

    assert world.client.post("/auth/account/register", json={**NEW_USER, "username": SYSTEM_USER_ID}).status_code == 409


# ---------------------------------------------------------------------------
# POST /admin/users
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("authentication", [True, False], ids=["auth-on", "auth-off"])
def test_an_administrator_creates_an_account_in_either_mode(world, authentication):
    admin = world.account("root", role="admin")
    world.set_authentication(authentication)
    headers = world.headers(admin) if authentication else {}

    created = world.client.post("/admin/users", json={**NEW_USER, "email": "alice@example.com"}, headers=headers)

    assert created.status_code == 201, created.text
    assert_conforms(created, "POST", "/admin/users")
    body = created.json()
    assert (body["username"], body["email"], body["role"], body["is_active"], body["pending_approval"]) == (
        "alice", "alice@example.com", "user", True, False,
    )
    with world.session() as session:
        stored = user_service.get_user_by_username(session, "alice")
        assert stored.password_hash != LONG_ENOUGH and user_auth.verify_password(LONG_ENOUGH, stored.password_hash)
    listed = world.client.get("/admin/users", headers=headers).json()["items"]
    assert sorted(u["username"] for u in listed) == ["alice", "root"]


def test_with_authentication_off_the_list_shows_the_created_accounts_and_never_the_synthetic_user(world):
    world.set_authentication(False)
    assert world.client.get("/admin/users").json()["items"] == []

    for name in ("alice", "bob"):
        assert world.client.post("/admin/users", json={**NEW_USER, "username": name}).status_code == 201

    names = [u["username"] for u in world.client.get("/admin/users").json()["items"]]
    assert sorted(names) == ["alice", "bob"] and SYSTEM_USER_ID not in names


def test_the_role_defaults_to_user_and_can_be_admin(world):
    world.set_authentication(False)

    assert world.client.post("/admin/users", json=NEW_USER).json()["role"] == "user"
    assert world.client.post("/admin/users", json={**NEW_USER, "username": "boss", "role": "admin"}).json()["role"] == "admin"


def test_a_taken_username_is_a_409_with_a_code_the_ui_translates(world):
    world.set_authentication(False)
    world.account("alice")

    taken = world.client.post("/admin/users", json=NEW_USER)

    assert taken.status_code == 409
    assert taken.json()["detail"]["error_code"] == "username_taken"
    assert_conforms(taken, "POST", "/admin/users")


def test_a_taken_email_is_a_409(world):
    world.set_authentication(False)
    assert world.client.post("/admin/users", json={**NEW_USER, "email": "a@example.com"}).status_code == 201

    taken = world.client.post("/admin/users", json={**NEW_USER, "username": "bob", "email": "a@example.com"})

    assert taken.status_code == 409 and taken.json()["detail"]["error_code"] == "email_taken"


def test_an_account_a_visitor_registered_blocks_the_name_for_the_administrator_too(world):
    world.set_authentication(True, registration_open=True)
    admin = world.account("root", role="admin")
    assert world.client.post("/auth/account/register", json=NEW_USER).status_code == 201

    taken = world.client.post("/admin/users", json=NEW_USER, headers=world.headers(admin))

    assert taken.status_code == 409


def test_the_synthetic_users_name_is_not_a_valid_username(world):
    world.set_authentication(False)

    assert world.client.post("/admin/users", json={**NEW_USER, "username": SYSTEM_USER_ID}).status_code == 422
    assert world.client.post("/admin/users", json={**NEW_USER, "username": f"  {SYSTEM_USER_ID}  "}).status_code == 422


@pytest.mark.parametrize("body", [
    pytest.param({"username": "", "password": LONG_ENOUGH}, id="empty-username"),
    pytest.param({"username": "   ", "password": LONG_ENOUGH}, id="blank-username"),
    pytest.param({"username": "a" * 151, "password": LONG_ENOUGH}, id="long-username"),
    pytest.param({"username": "alice", "password": "short"}, id="short-password"),
    pytest.param({"username": "alice", "password": LONG_ENOUGH, "role": "owner"}, id="unknown-role"),
    pytest.param({"username": "alice", "password": LONG_ENOUGH, "email": "not-an-email"}, id="bad-email"),
    pytest.param({"username": "alice", "password": LONG_ENOUGH, "is_active": False}, id="unknown-field"),
    pytest.param({"password": LONG_ENOUGH}, id="no-username"),
    pytest.param({"username": "alice"}, id="no-password"),
])
def test_an_invalid_body_is_a_422_and_creates_nothing(world, body):
    world.set_authentication(False)

    assert world.client.post("/admin/users", json=body).status_code == 422
    with world.session() as session:
        assert user_service.get_user_count(session) == 0


def test_the_username_is_stored_trimmed_and_an_empty_email_is_none(world):
    world.set_authentication(False)

    body = world.client.post("/admin/users", json={**NEW_USER, "username": "  alice  ", "email": "  "}).json()

    assert (body["username"], body["email"]) == ("alice", None)


def test_only_an_administrator_creates_accounts(world):
    world.set_authentication(True)
    user = world.account("alice")

    assert world.client.post("/admin/users", json={**NEW_USER, "username": "bob"}).status_code == 401
    assert world.client.post("/admin/users", json={**NEW_USER, "username": "bob"}, headers=world.headers(user)).status_code == 403
    with world.session() as session:
        assert [u.username for u in session.exec(select(User).where(User.id != SYSTEM_USER_ID)).all()] == ["alice"]


def test_an_account_created_with_authentication_off_signs_in_once_it_is_on(world):
    world.set_authentication(False)
    assert world.client.post("/admin/users", json={**NEW_USER, "username": "boss", "role": "admin"}).status_code == 201
    world.set_authentication(True)

    login = world.client.post("/auth/account/login", json={"username": "boss", "password": NEW_USER["password"]})

    assert login.status_code == 200 and login.json()["access_token"]


# ---------------------------------------------------------------------------
# Turning authentication on needs someone who can sign in
# ---------------------------------------------------------------------------


def _turn_on(world, **extra):
    return world.client.patch("/settings", json={"auth_enabled": True, **extra})


def test_authentication_cannot_be_turned_on_without_an_administrator(world):
    world.set_authentication(False)

    refused = _turn_on(world, save_cover=False)

    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"]["error_code"] == "no_admin_account"
    assert_conforms(refused, "PATCH", "/settings")
    assert world.config.auth_enabled is False
    assert world.config.get("save_cover") is not False  # the whole batch was refused


@pytest.mark.parametrize("fields", [
    pytest.param({"role": "user"}, id="plain-user"),
    pytest.param({"role": "admin", "active": False}, id="inactive-admin"),
    pytest.param({"role": "admin", "pending": True, "active": False}, id="admin-waiting-for-approval"),
    pytest.param({"role": "admin", "password": None}, id="admin-without-a-password"),
])
def test_an_account_that_cannot_sign_in_as_an_administrator_does_not_count(world, fields):
    world.set_authentication(False)
    world.account("someone", **fields)

    refused = _turn_on(world)

    assert refused.status_code == 400 and refused.json()["detail"]["error_code"] == "no_admin_account"
    assert world.config.auth_enabled is False


def test_authentication_is_turned_on_once_an_administrator_exists(world):
    world.set_authentication(False)
    world.account("root", role="admin")

    done = _turn_on(world)

    assert done.status_code == 200, done.text
    assert world.config.auth_enabled is True
    assert world.client.get("/auth/account/status").json()["mode"] == "guest"


def test_an_administrator_created_through_the_api_is_what_turning_it_on_needs(world):
    world.set_authentication(False)
    assert _turn_on(world).status_code == 400

    assert world.client.post("/admin/users", json={**NEW_USER, "role": "admin"}).status_code == 201

    assert _turn_on(world).status_code == 200


@pytest.mark.parametrize("secret", ["secret", "short", None], ids=["placeholder", "short", "unset"])
def test_the_secret_rule_still_applies_with_an_administrator_and_is_reported_first_without_one(world, monkeypatch, secret):
    world.set_authentication(False)
    if secret is None:
        monkeypatch.delenv("MUSIZEER_AUTH_SECRET")
    else:
        monkeypatch.setenv("MUSIZEER_AUTH_SECRET", secret)

    assert _turn_on(world).json()["detail"]["error_code"] == "auth_secret_insecure"  # no administrator yet
    world.account("root", role="admin")
    assert _turn_on(world).json()["detail"]["error_code"] == "auth_secret_insecure"  # an administrator now
    assert world.config.auth_enabled is False


def test_turning_it_off_never_asks_for_an_administrator_nor_a_secret(world, monkeypatch):
    from services.settings import SettingsService

    world.set_authentication(True)  # no account at all, and no good secret: nobody could sign in
    monkeypatch.delenv("MUSIZEER_AUTH_SECRET")
    stand_in = User(id="stand-in", username="stand-in", password_hash="x", role="admin")

    with world.session() as session:
        SettingsService(session, world.config, world.registry).write_values({"auth_enabled": False}, stand_in)

    assert world.config.auth_enabled is False


def test_a_write_that_keeps_authentication_on_is_an_ordinary_write(world):
    world.account("root", role="admin")
    world.set_authentication(True)
    other = world.account("second", role="admin")

    assert world.client.patch("/settings", json={"auth_enabled": True}, headers=world.headers(other)).status_code == 200
    assert world.config.auth_enabled is True


def test_the_check_is_declared_in_the_catalogue_with_the_secret_rule_and_never_sent_to_the_ui():
    from core.settings_catalog import get_def

    setting = get_def("auth_enabled")
    assert setting.guard_on is not None and setting.guard_on[0] is True
    assert "guard_on" not in setting.to_public()
