"""
ONE password rule (``core.user_auth.hash_new_password``, answered by ``routes/_password.py``) for every path
that SETS a password: registration, an account made by an administrator (which is also the guided
administrator step of the settings page), an administrator changing a password, and the first administrator
of a deployment. A password that is too short is a 422 ``password_too_short`` with the minimum. The rule
applies only when a password is set: an account that has a shorter one keeps signing in.
"""
from __future__ import annotations

import logging

import pytest

import main
from core import user_auth
from db import user_service

from tests.auth_modes_support import LONG_ENOUGH, world  # noqa: F401
from tests.test_openapi_responses import assert_conforms

MIN = user_auth.MIN_PASSWORD_LENGTH
SHORT, EXACT = "x" * (MIN - 1), "x" * MIN


def _weak(response) -> None:
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "password_too_short" and detail["min_length"] == MIN
    assert str(MIN) in detail["error_message"]


def test_the_rule_is_eight_characters_and_lives_in_one_function():
    assert MIN == 8
    with pytest.raises(user_auth.WeakPassword):
        user_auth.hash_new_password(SHORT)
    assert user_auth.verify_password(EXACT, user_auth.hash_new_password(EXACT))
    assert user_auth.WeakPassword.error_code == "password_too_short"


def test_registration_applies_it(world):
    world.set_authentication(True, registration_open=True)

    refused = world.client.post("/auth/account/register", json={"username": "alice", "password": SHORT})
    _weak(refused)
    assert_conforms(refused, "POST", "/auth/account/register")
    with world.session() as session:
        assert user_service.get_user_by_username(session, "alice") is None

    assert world.client.post("/auth/account/register", json={"username": "alice", "password": EXACT}).status_code == 201


@pytest.mark.parametrize("role", ["user", "admin"])
def test_an_administrator_creating_an_account_applies_it_and_so_does_the_guided_step(world, role):
    """The guided step of the settings page creates its administrator through this same route."""
    world.set_authentication(False)

    _weak(world.client.post("/admin/users", json={"username": "boss", "password": SHORT, "role": role}))
    with world.session() as session:
        assert user_service.get_user_count(session) == 0

    assert world.client.post("/admin/users", json={"username": "boss", "password": EXACT, "role": role}).status_code == 201


def test_an_administrator_changing_a_password_applies_it(world):
    admin = world.account("root", role="admin")
    alice = world.account("alice")
    world.set_authentication(True)
    headers = world.headers(admin)

    refused = world.client.patch(f"/admin/users/{alice.id}", json={"password": SHORT}, headers=headers)
    _weak(refused)
    with world.session() as session:  # nothing changed, not even the other fields of the same request
        assert user_auth.verify_password(LONG_ENOUGH, user_service.get_user_by_id(session, alice.id).password_hash)

    done = world.client.patch(f"/admin/users/{alice.id}", json={"password": "a-new-password"}, headers=headers)
    assert done.status_code == 200
    assert world.client.post("/auth/account/login", json={"username": "alice", "password": "a-new-password"}).status_code == 200
    assert world.client.post("/auth/account/login", json={"username": "alice", "password": LONG_ENOUGH}).status_code == 401


def test_a_change_that_sets_no_password_is_not_asked_for_one(world):
    admin = world.account("root", role="admin")
    alice = world.account("alice", password="short")  # an old, shorter password
    world.set_authentication(True)

    done = world.client.patch(f"/admin/users/{alice.id}", json={"is_active": False}, headers=world.headers(admin))

    assert done.status_code == 200


def test_an_existing_shorter_password_still_signs_in(world):
    world.account("old", password="abc")  # made before the rule, or by a script
    world.set_authentication(True)

    login = world.client.post("/auth/account/login", json={"username": "old", "password": "abc"})

    assert login.status_code == 200 and login.json()["access_token"]


def test_the_first_administrator_of_a_deployment_applies_it_too(world, monkeypatch, caplog):
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAIL", "boss@example.com")
    monkeypatch.setattr("db.engine.get_session", lambda: iter([world.session()]))

    monkeypatch.setenv("ADMIN_BOOTSTRAP_PASSWORD", SHORT)
    with caplog.at_level(logging.ERROR):
        main._bootstrap_admin()
    with world.session() as session:
        assert user_service.get_user_count(session) == 0
    assert "no administrator was created" in caplog.text

    monkeypatch.setenv("ADMIN_BOOTSTRAP_PASSWORD", EXACT)
    main._bootstrap_admin()
    with world.session() as session:
        assert [(u.username, u.role) for u in user_service.list_users(session)] == [("boss", "admin")]
