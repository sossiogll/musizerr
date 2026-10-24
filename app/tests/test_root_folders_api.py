"""
The routes of the root folders (ADR-0008) through the real dependency chain: admin only in the four modes, declaring,
renaming, moving and removing a root, the gateway's verdict coming back with the push, the explicit Check and the folder
picker. A fake gateway answers; no real provider.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.user_auth import create_access_token
from db import root_store
from db.engine import get_session
from db.models import RootFolder, User
from routes import deps
from routes import roots as roots_routes
from routes.roots import router as roots_router
from services.config_manager import ConfigManager
from services.gateway_client import GatewayClient
from tests.conftest import FakeRegistry
from tests.folders_support import FolderStorage, add_root, store_folder, store_provider_folder
from tests.sources_support import FakeControlSocket


def _user(engine, role: str) -> dict:
    with Session(engine) as session:
        user = User(username=f"{role}-user", password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}


@pytest.fixture
def world(engine, monkeypatch):
    gateway = FakeControlSocket()
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})
    registry = FakeRegistry(storage=[FolderStorage()])
    app = FastAPI()
    app.include_router(roots_router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    monkeypatch.setattr(roots_routes, "_gateway", lambda: GatewayClient(gateway.path))
    world = type("World", (), {})()
    with TestClient(app) as client:
        world.client, world.gateway, world.engine, world.config = client, gateway, engine, config
        world.admin, world.user = _user(engine, "admin"), _user(engine, "user")
        yield world
    gateway.close()


def code(response) -> str:
    return response.json()["detail"]["error_code"]


def roots_in_db(engine) -> list[RootFolder]:
    with Session(engine) as session:
        return root_store.list_roots(session)


# ------------------------------------------------------------------
# Who may
# ------------------------------------------------------------------

ROUTES = [
    ("get", "/roots"), ("post", "/roots"), ("get", "/roots/browse"), ("patch", "/roots/x"), ("delete", "/roots/x"),
    ("get", "/roots/x/check"),
]


@pytest.mark.parametrize("method, path", ROUTES)
def test_no_one_but_an_administrator_reaches_the_root_folders(world, method, path):
    body = {"json": {"path": "/library"}} if method in ("post", "patch") else {}

    assert getattr(world.client, method)(path, **body).status_code == 401
    assert getattr(world.client, method)(path, headers=world.user, **body).status_code == 403


def test_with_authentication_off_the_owner_administers_the_root_folders(world):
    world.config.update({"auth_enabled": False})

    assert world.client.get("/roots").status_code == 200


# ------------------------------------------------------------------
# Declaring
# ------------------------------------------------------------------


def test_a_new_install_has_no_roots_and_no_folder_set(world):
    body = world.client.get("/roots", headers=world.admin).json()

    assert body["roots"] == []
    by_key = {folder["key"]: folder for folder in body["folders"]}
    assert by_key["workspace_dir"]["state"] == "not_set" and by_key["workspace_dir"]["suggested_path"] == ".musizeer-work"
    assert by_key["provider.foldered.root_dir"]["suggested_path"] == "Music"


def test_declaring_a_root_saves_it_pushes_it_to_the_gateway_and_returns_its_verdict(world):
    reply = world.client.post("/roots", json={"path": "/library/", "label": "  The music  "}, headers=world.admin)

    assert reply.status_code == 201
    root = reply.json()
    assert (root["label"], root["path"], root["state"], root["reason"]) == ("The music", "/library", "accepted", "")
    assert root["checked_at"] is not None and root["used_by"] == []
    assert [r["path"] for r in world.gateway.roots] == ["/library"] and world.gateway.roots[0]["id"] == root["id"]
    assert [r.path for r in roots_in_db(world.engine)] == ["/library"]


def test_a_root_without_a_label_gets_the_last_component_of_its_path(world):
    assert world.client.post("/roots", json={"path": "/media/music"}, headers=world.admin).json()["label"] == "Music"


def test_a_root_the_gateway_refuses_is_saved_with_its_reason_and_not_used(world):
    world.gateway.refused_paths["/etc"] = "it is part of the container's own file system, not a mounted volume"

    root = world.client.post("/roots", json={"path": "/etc"}, headers=world.admin).json()

    assert root["state"] == "refused" and "not a mounted volume" in root["reason"]
    assert [r.state for r in roots_in_db(world.engine)] == ["refused"]


@pytest.mark.parametrize("path", ["", "relative", "/", "/a/../b", "/a//b", "x" * 5000])
def test_a_path_that_is_not_an_absolute_clean_path_is_422_and_nothing_is_saved(world, path):
    reply = world.client.post("/roots", json={"path": path}, headers=world.admin)

    assert reply.status_code == 422 and code(reply) == "root_path_invalid"
    assert roots_in_db(world.engine) == [] and world.gateway.pushes() == []


def test_the_same_path_twice_is_409(world):
    world.client.post("/roots", json={"path": "/library"}, headers=world.admin)

    reply = world.client.post("/roots", json={"path": "/library/", "label": "Again"}, headers=world.admin)

    assert reply.status_code == 409 and code(reply) == "root_path_in_use" and len(roots_in_db(world.engine)) == 1


def test_one_root_may_lie_inside_another(world):
    assert world.client.post("/roots", json={"path": "/media"}, headers=world.admin).status_code == 201
    assert world.client.post("/roots", json={"path": "/media/Musica"}, headers=world.admin).status_code == 201


def test_a_label_that_is_too_long_is_422(world):
    reply = world.client.post("/roots", json={"path": "/library", "label": "x" * 100}, headers=world.admin)

    assert reply.status_code == 422 and code(reply) == "root_label_invalid"


def test_a_gateway_that_does_not_answer_still_saves_the_root_unchecked(world):
    world.gateway.close()

    reply = world.client.post("/roots", json={"path": "/library"}, headers=world.admin)

    assert reply.status_code == 201 and reply.json()["state"] == "unchecked" and reply.json()["checked_at"] is None


# ------------------------------------------------------------------
# What is listed
# ------------------------------------------------------------------


def test_the_list_says_which_folder_settings_use_each_root(world):
    root = add_root(world.engine, "/library")
    store_provider_folder(world.engine, "foldered", "root_dir", root, "Music")
    store_folder(world.engine, "workspace_dir", root, ".musizeer-work")

    body = world.client.get("/roots", headers=world.admin).json()

    (listed,) = body["roots"]
    assert sorted(item["key"] for item in listed["used_by"]) == ["provider.foldered.root_dir", "workspace_dir"]
    by_key = {folder["key"]: folder for folder in body["folders"]}
    assert by_key["provider.foldered.root_dir"]["resolved"] == "/library/Music" and by_key["provider.foldered.root_dir"]["state"] == "set"
    assert by_key["workspace_dir"]["resolved"] == "/library/.musizeer-work"


def test_a_folder_whose_root_the_gateway_refuses_is_listed_as_not_set_with_the_reason(world):
    root = add_root(world.engine, "/etc", state="refused", reason="not a mounted volume")
    store_folder(world.engine, "workspace_dir", root, "work")

    folder = {f["key"]: f for f in world.client.get("/roots", headers=world.admin).json()["folders"]}["workspace_dir"]

    assert folder["state"] == "root_refused" and folder["detail"] == "not a mounted volume" and folder["resolved"] is None


# ------------------------------------------------------------------
# Renaming, moving, removing
# ------------------------------------------------------------------


def test_a_root_is_renamed_without_a_push(world):
    root = add_root(world.engine, "/library")

    reply = world.client.patch(f"/roots/{root.id}", json={"label": "Songs"}, headers=world.admin)

    assert reply.status_code == 200 and reply.json()["label"] == "Songs" and reply.json()["path"] == "/library"
    assert world.gateway.pushes() == []


def test_a_root_that_is_not_in_use_moves_at_once_and_is_judged_again(world):
    root = add_root(world.engine, "/library")
    world.gateway.refused_paths["/music"] = "not writable"

    reply = world.client.patch(f"/roots/{root.id}", json={"path": "/music/"}, headers=world.admin)

    assert reply.status_code == 200
    assert (reply.json()["path"], reply.json()["state"], reply.json()["reason"]) == ("/music", "refused", "not writable")
    assert [r["path"] for r in world.gateway.roots] == ["/music"]


def test_moving_a_root_in_use_asks_for_a_confirmation_that_lists_what_depends_on_it(world):
    root = add_root(world.engine, "/library")
    store_provider_folder(world.engine, "foldered", "root_dir", root, "Music")

    reply = world.client.patch(f"/roots/{root.id}", json={"path": "/media/library"}, headers=world.admin)

    assert reply.status_code == 409 and code(reply) == "confirmation_required"
    assert [item["key"] for item in reply.json()["detail"]["used_by"]] == ["provider.foldered.root_dir"]
    assert roots_in_db(world.engine)[0].path == "/library" and world.gateway.pushes() == []

    confirmed = world.client.patch(f"/roots/{root.id}?confirm=true", json={"path": "/media/library"}, headers=world.admin)

    assert confirmed.status_code == 200 and confirmed.json()["path"] == "/media/library"
    assert [r["path"] for r in world.gateway.roots] == ["/media/library"]
    assert [item["key"] for item in confirmed.json()["used_by"]] == ["provider.foldered.root_dir"]


def test_a_root_cannot_move_onto_the_path_of_another(world):
    first, second = add_root(world.engine, "/a"), add_root(world.engine, "/b")

    reply = world.client.patch(f"/roots/{second.id}", json={"path": "/a"}, headers=world.admin)

    assert reply.status_code == 409 and code(reply) == "root_path_in_use"
    assert {r.path for r in roots_in_db(world.engine)} == {"/a", "/b"} and first.id != second.id


def test_a_root_in_use_cannot_be_removed_and_the_settings_that_use_it_are_listed(world):
    root = add_root(world.engine, "/library")
    store_folder(world.engine, "workspace_dir", root, ".musizeer-work")

    reply = world.client.delete(f"/roots/{root.id}", headers=world.admin)

    assert reply.status_code == 409 and code(reply) == "root_in_use"
    assert [item["key"] for item in reply.json()["detail"]["used_by"]] == ["workspace_dir"]
    assert len(roots_in_db(world.engine)) == 1


def test_a_root_nobody_uses_is_removed_and_the_gateway_is_told(world):
    root = add_root(world.engine, "/library")

    reply = world.client.delete(f"/roots/{root.id}", headers=world.admin)

    assert reply.status_code == 200 and reply.json() == {"ok": True}
    assert roots_in_db(world.engine) == [] and world.gateway.roots == []


def test_a_root_that_is_not_there_is_404(world):
    for call in (
        lambda: world.client.patch("/roots/nope", json={"label": "x"}, headers=world.admin),
        lambda: world.client.delete("/roots/nope", headers=world.admin),
        lambda: world.client.get("/roots/nope/check", headers=world.admin),
    ):
        reply = call()
        assert reply.status_code == 404 and code(reply) == "root_not_found"


# ------------------------------------------------------------------
# Check and browse
# ------------------------------------------------------------------


def test_the_check_asks_the_gateway_with_a_write_test_and_says_what_the_application_sees(world, tmp_path):
    root = add_root(world.engine, str(tmp_path))

    report = world.client.get(f"/roots/{root.id}/check", headers=world.admin).json()

    assert report["ok"] is True and report["gateway"]["mounted"] and report["gateway"]["write_test"] == {"ok": True, "error": ""}
    assert report["application"] == {"exists": True, "writable": True}
    assert report["root"]["id"] == root.id
    assert {"op": "inspect", "path": str(tmp_path), "write_test": True} in world.gateway.requests


def test_the_check_of_a_root_the_gateway_says_is_not_mounted_is_not_ok(world):
    root = add_root(world.engine, "/etc")
    world.gateway.inspections["/etc"] = {
        "ok": True, "path": "/etc", "exists": True, "directory": True, "writable": False, "mounted": False,
        "reason": "it is part of the container's own file system", "device": None, "mount_id": None, "mount_point": "/",
        "fs_type": "overlay", "free_bytes": 1, "real_path": "/etc",
    }

    report = world.client.get(f"/roots/{root.id}/check", headers=world.admin).json()

    assert report["ok"] is False and report["gateway"]["mounted"] is False


def test_the_check_says_when_the_gateway_could_not_be_asked(world):
    root = add_root(world.engine, "/library")
    world.gateway.close()

    report = world.client.get(f"/roots/{root.id}/check", headers=world.admin).json()

    assert report["gateway"] is None and report["gateway_error"] and report["ok"] is False


def test_the_picker_browses_through_the_gateway(world):
    world.gateway.browsings["/library"] = {
        "ok": True, "path": "/library", "parent": None, "volume": {"mount_point": "/library"}, "truncated": False,
        "directories": [{"name": "Rock", "path": "/library/Rock", "link": False}],
    }

    reply = world.client.get("/roots/browse", params={"path": "/library"}, headers=world.admin)

    assert reply.status_code == 200 and reply.json()["directories"][0]["path"] == "/library/Rock"
    assert {"op": "browse", "path": "/library"} in world.gateway.requests


def test_without_a_path_the_picker_lists_the_volumes(world):
    reply = world.client.get("/roots/browse", headers=world.admin)

    assert reply.status_code == 200 and {"op": "browse", "path": None} in world.gateway.requests


def test_a_path_the_gateway_refuses_to_browse_is_422_with_its_reason(world):
    world.gateway.browsings["/etc"] = {"ok": False, "error": "it is part of the container's own file system, not a mounted volume"}

    reply = world.client.get("/roots/browse", params={"path": "/etc"}, headers=world.admin)

    assert reply.status_code == 422 and code(reply) == "gateway_refused"
    assert "not a mounted volume" in reply.json()["detail"]["error_message"]


def test_the_picker_says_503_when_the_gateway_does_not_answer(world):
    world.gateway.close()

    reply = world.client.get("/roots/browse", headers=world.admin)

    assert reply.status_code == 503 and code(reply) == "gateway_unreachable"


# ------------------------------------------------------------------
# The documented answers are the real ones
# ------------------------------------------------------------------


def test_every_answer_conforms_to_the_documented_one(world, tmp_path):
    from tests.test_openapi_responses import assert_conforms

    def check(reply, method, path):
        assert_conforms(reply, method, path)
        return reply

    admin = world.admin
    created = check(world.client.post("/roots", json={"path": str(tmp_path), "label": "Tmp"}, headers=admin), "POST", "/roots")
    root_id = created.json()["id"]
    check(world.client.post("/roots", json={"path": "relative"}, headers=admin), "POST", "/roots")
    check(world.client.post("/roots", json={"path": str(tmp_path)}, headers=admin), "POST", "/roots")
    check(world.client.get("/roots", headers=admin), "GET", "/roots")
    check(world.client.get(f"/roots/{root_id}/check", headers=admin), "GET", "/roots/{root_id}/check")
    check(world.client.patch(f"/roots/{root_id}", json={"label": "Songs"}, headers=admin), "PATCH", "/roots/{root_id}")
    check(world.client.patch(f"/roots/{root_id}", json={"path": "/elsewhere"}, headers=admin), "PATCH", "/roots/{root_id}")
    check(world.client.patch("/roots/nope", json={"label": "x"}, headers=admin), "PATCH", "/roots/{root_id}")
    check(world.client.get("/roots/browse", params={"path": "/library"}, headers=admin), "GET", "/roots/browse")
    world.gateway.browsings["/etc"] = {"ok": False, "error": "no"}
    check(world.client.get("/roots/browse", params={"path": "/etc"}, headers=admin), "GET", "/roots/browse")
    store_folder(world.engine, "workspace_dir", RootFolder(id=root_id, label="x", path="x"), "w")
    check(world.client.delete(f"/roots/{root_id}", headers=admin), "DELETE", "/roots/{root_id}")
