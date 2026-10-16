"""
The routes of the sources, through the real dependency chain: admin only in the four modes (no credentials, a
user, an admin, authentication off), the error codes of every refusal, and no response ever holding a token.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.user_auth import create_access_token
from db import user_service
from db.engine import get_session
from db.models import User
from routes import deps
from routes.sources import router as sources_router
from services.config_manager import ConfigManager
from services.gateway_client import GatewayClient
from services.gateway_sync import GatewaySync
from services.sources import CatalogFetcher, SourceManager
from tests.conftest import FakeRegistry
from tests.sources_support import (
    DIGEST_A,
    DIGEST_B,
    TOKEN,
    FakeCatalogServer,
    FakeControlSocket,
    catalog,
    entry,
)

URL = "https://catalog.example/registry.json"
PRIVATE_URL = "https://private.example/catalog.json"


def _user(engine, role: str) -> dict:
    with Session(engine) as session:
        user = User(username=f"{role}-user", password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}


@pytest.fixture
def world(engine):
    server, gateway = FakeCatalogServer(), FakeControlSocket()
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})
    announcements: list[str] = []
    app = FastAPI()
    app.include_router(sources_router)

    def _session():
        with Session(engine) as s:
            yield s

    def _manager():
        with Session(engine) as s:
            yield SourceManager(
                s, announce_change=lambda: announcements.append("changed"),
                gateway=GatewaySync(GatewayClient(gateway.path), lambda: Session(engine)),
                fetcher=CatalogFetcher(server.transport),
            )

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: FakeRegistry()
    app.dependency_overrides[deps.get_source_manager] = _manager
    world = type("World", (), {})()
    with TestClient(app) as client:
        world.client, world.server, world.gateway, world.engine, world.config = client, server, gateway, engine, config
        world.announcements = announcements
        world.admin = _user(engine, "admin")
        world.user = _user(engine, "user")
        yield world
    gateway.close()


def serve(world, url=URL, document=None, **kwargs):
    world.server.serve(url, document or catalog(entry()))
    return world.client.post("/sources", json={"url": url, **kwargs}, headers=world.admin)


def code(response) -> str:
    return response.json()["detail"]["error_code"]


ROUTES = [
    ("GET", "/sources", None),
    ("POST", "/sources", {"url": URL}),
    ("PATCH", "/sources/x", {"enabled": False}),
    ("DELETE", "/sources/x", None),
    ("GET", "/sources/x/catalog", None),
    ("GET", "/sources/providers/installed", None),
    ("GET", "/sources/providers/gateway", None),
    ("POST", "/sources/providers/install", {"source_id": "x", "provider_name": "lrclib", "digest": DIGEST_A}),
    ("POST", "/sources/providers/lrclib/update", {"digest": DIGEST_A}),
    ("DELETE", "/sources/providers/lrclib", None),
]


@pytest.mark.parametrize("method, path, body", ROUTES)
def test_every_route_is_admin_only(world, method, path, body):
    send = lambda headers: world.client.request(method, path, json=body, headers=headers)  # noqa: E731

    assert send({}).status_code == 401, "no credentials"
    assert send(world.user).status_code == 403, "a user"
    assert send(world.admin).status_code in (200, 201, 404, 409, 422, 502), "an admin gets as far as the work"

    world.config.update({"auth_enabled": False})
    assert send({}).status_code != 401, "with authentication off the instance is open"
    assert send({}).status_code != 403


def test_a_user_changes_nothing(world):
    assert world.client.post("/sources", json={"url": URL}, headers=world.user).status_code == 403
    assert world.client.get("/sources", headers=world.admin).json() == []
    assert world.server.requests == []


def test_adding_listing_editing_and_removing_a_source(world):
    created = serve(world, credential=None)

    assert created.status_code == 201
    source = created.json()
    assert set(source) == {"id", "name", "url", "enabled", "added_at", "has_credential", "allow_insecure_registry"}
    assert source["name"] == "Test catalog" and source["has_credential"] is False
    assert world.client.get("/sources", headers=world.admin).json() == [source]

    patched = world.client.patch(f"/sources/{source['id']}", json={"enabled": False}, headers=world.admin)
    assert patched.status_code == 200 and patched.json()["enabled"] is False
    assert world.client.patch(f"/sources/{source['id']}", json={}, headers=world.admin).json()["enabled"] is False, "nothing named, nothing changed"

    removed = world.client.delete(f"/sources/{source['id']}", headers=world.admin)
    assert removed.json() == {"ok": True, "uninstalled": []}
    assert world.client.get("/sources", headers=world.admin).json() == []


def test_the_refusals_have_their_codes_and_statuses(world):
    serve(world)

    cases = [
        (world.client.post("/sources", json={"url": URL}, headers=world.admin), 409, "source_exists"),
        (world.client.post("/sources", json={"url": "ftp://x"}, headers=world.admin), 422, "invalid_source_url"),
        (world.client.post("/sources", json={"url": "http://x.example/c.json", "credential": "t"}, headers=world.admin), 422, "credential_requires_https"),
        (world.client.post("/sources", json={"url": PRIVATE_URL, "credential": "t", "allow_insecure_registry": True}, headers=world.admin), 422, "credential_with_insecure_registry"),
        (world.client.post("/sources", json={"url": PRIVATE_URL}, headers=world.admin), 502, "catalog_not_found"),
        (world.client.patch("/sources/nope", json={"enabled": True}, headers=world.admin), 404, "source_not_found"),
        (world.client.delete("/sources/nope", headers=world.admin), 404, "source_not_found"),
        (world.client.get("/sources/nope/catalog", headers=world.admin), 404, "source_not_found"),
        (world.client.delete("/sources/providers/ghost", headers=world.admin), 404, "not_installed"),
        (world.client.post("/sources/providers/ghost/update", json={"digest": DIGEST_A}, headers=world.admin), 404, "not_installed"),
    ]
    for response, status, error_code in cases:
        assert (response.status_code, code(response)) == (status, error_code), response.text
        assert response.json()["detail"]["error_message"]


def test_a_catalog_that_is_not_valid_is_a_422_with_the_reason(world):
    world.server.serve(URL, {"name": "x", "version": 1, "providers": []})

    response = world.client.post("/sources", json={"url": URL}, headers=world.admin)

    assert response.status_code == 422 and code(response) == "catalog_invalid"
    assert "version 1" in response.json()["detail"]["error_message"]


def test_installing_updating_and_uninstalling_through_the_routes(world):
    source = serve(world).json()
    catalog_page = world.client.get(f"/sources/{source['id']}/catalog", headers=world.admin)
    assert catalog_page.status_code == 200 and catalog_page.json()["entries"][0]["digest"] == DIGEST_A

    refused = world.client.post("/sources/providers/install", json={"source_id": source["id"], "provider_name": "lrclib", "digest": DIGEST_B}, headers=world.admin)
    assert (refused.status_code, code(refused)) == (409, "catalog_changed")

    installed = world.client.post("/sources/providers/install", json={"source_id": source["id"], "provider_name": "lrclib", "digest": DIGEST_A}, headers=world.admin)
    assert installed.status_code == 201
    body = installed.json()
    assert (body["name"], body["digest"], body["roles"], body["source_name"]) == ("lrclib", DIGEST_A, ["lyrics"], "Test catalog")
    assert body["approved_by"] is not None and body["permissions"]["egress"] == ["lrclib.net:443"]
    assert world.announcements == ["changed"] and world.gateway.pushes()[-1]["providers"][0]["name"] == "lrclib"
    again = world.client.post("/sources/providers/install", json={"source_id": source["id"], "provider_name": "lrclib", "digest": DIGEST_A}, headers=world.admin)
    assert (again.status_code, code(again)) == (409, "already_installed")

    world.server.serve(URL, catalog(entry("lrclib", DIGEST_B, permissions={"egress": ["lrclib.net:443", "new.example:443"]})))
    listing = world.client.get("/sources/providers/installed", headers=world.admin).json()
    assert listing[0]["update"]["diff"]["egress"]["added"] == ["new.example:443"]
    assert world.client.get("/sources/providers/installed?updates=false", headers=world.admin).json()[0]["update"] is None

    wrong = world.client.post("/sources/providers/lrclib/update", json={"digest": DIGEST_A}, headers=world.admin)
    assert (wrong.status_code, code(wrong)) == (409, "catalog_changed")
    updated = world.client.post("/sources/providers/lrclib/update", json={"digest": DIGEST_B}, headers=world.admin)
    assert updated.status_code == 200 and updated.json()["digest"] == DIGEST_B
    current = world.client.post("/sources/providers/lrclib/update", json={"digest": DIGEST_B}, headers=world.admin)
    assert (current.status_code, code(current)) == (409, "already_current")

    assert world.client.delete("/sources/providers/lrclib", headers=world.admin).json() == {"ok": True}
    assert world.gateway.pushes()[-1]["providers"] == []


def test_removing_a_source_names_the_providers_that_went_with_it(world):
    source = serve(world).json()
    world.client.post("/sources/providers/install", json={"source_id": source["id"], "provider_name": "lrclib", "digest": DIGEST_A}, headers=world.admin)

    removed = world.client.delete(f"/sources/{source['id']}", headers=world.admin)

    assert removed.json() == {"ok": True, "uninstalled": ["lrclib"]}


def test_the_gateway_state_route_reports_the_gateway_and_the_hosts_refusals(world):
    source = serve(world).json()
    world.client.post("/sources/providers/install", json={"source_id": source["id"], "provider_name": "lrclib", "digest": DIGEST_A}, headers=world.admin)

    state = world.client.get("/sources/providers/gateway", headers=world.admin).json()

    assert state["reachable"] and state["status"] == "ok" and state["providers"][0]["name"] == "lrclib"
    assert state["host_rejected"] == {}
    world.gateway.close()
    assert world.client.get("/sources/providers/gateway", headers=world.admin).json()["reachable"] is False


def test_no_response_ever_holds_the_token(world):
    world.server.require_token[PRIVATE_URL] = TOKEN
    created = serve(world, url=PRIVATE_URL, credential=TOKEN)
    source = created.json()
    assert source["has_credential"] is True
    responses = [
        created,
        world.client.get("/sources", headers=world.admin),
        world.client.patch(f"/sources/{source['id']}", json={"credential": TOKEN}, headers=world.admin),
        world.client.get(f"/sources/{source['id']}/catalog", headers=world.admin),
        world.client.post("/sources/providers/install", json={"source_id": source["id"], "provider_name": "lrclib", "digest": DIGEST_A}, headers=world.admin),
        world.client.get("/sources/providers/installed", headers=world.admin),
        world.client.get("/sources/providers/gateway", headers=world.admin),
        world.client.post("/sources", json={"url": PRIVATE_URL, "credential": TOKEN}, headers=world.admin),  # a duplicate: an error
        world.client.post("/sources", json={"url": "http://plain.example/c.json", "credential": TOKEN}, headers=world.admin),
        world.client.delete(f"/sources/{source['id']}", headers=world.admin),
    ]

    for response in responses:
        assert TOKEN not in response.text and TOKEN not in json.dumps(dict(response.headers)), response.request.url
    installed = responses[5].json()[0]
    assert installed["sends_source_token"] is True and "credential" not in installed
    assert "credential" not in json.dumps(responses[1].json()).replace("has_credential", "")


def test_a_token_can_be_replaced_and_removed_with_null(world):
    world.server.require_token[PRIVATE_URL] = TOKEN
    source = serve(world, url=PRIVATE_URL, credential=TOKEN).json()

    cleared = world.client.patch(f"/sources/{source['id']}", json={"credential": None}, headers=world.admin)

    assert cleared.status_code == 200 and cleared.json()["has_credential"] is False


def test_every_response_conforms_to_the_openapi_spec(world):
    from tests.test_openapi_responses import assert_conforms

    world.server.require_token[PRIVATE_URL] = TOKEN
    source = serve(world, url=PRIVATE_URL, credential=TOKEN)
    assert_conforms(source, "POST", "/sources")
    sid = source.json()["id"]
    install = {"source_id": sid, "provider_name": "lrclib", "digest": DIGEST_A}
    world.server.serve(PRIVATE_URL, catalog(entry(), entry("old", provider_api_version=1), entry("broken", digest="x")))

    checks = [
        (world.client.get("/sources", headers=world.admin), "GET", "/sources"),
        (world.client.get(f"/sources/{sid}/catalog", headers=world.admin), "GET", "/sources/{source_id}/catalog"),
        (world.client.post("/sources", json={"url": PRIVATE_URL, "credential": TOKEN}, headers=world.admin), "POST", "/sources"),
        (world.client.post("/sources/providers/install", json=install, headers=world.admin), "POST", "/sources/providers/install"),
        (world.client.post("/sources/providers/install", json=install, headers=world.admin), "POST", "/sources/providers/install"),
        (world.client.get("/sources/providers/installed", headers=world.admin), "GET", "/sources/providers/installed"),
        (world.client.get("/sources/providers/gateway", headers=world.admin), "GET", "/sources/providers/gateway"),
        (world.client.patch(f"/sources/{sid}", json={"enabled": False}, headers=world.admin), "PATCH", "/sources/{source_id}"),
        (world.client.get(f"/sources/{sid}/catalog", headers=world.admin), "GET", "/sources/{source_id}/catalog"),
        (world.client.patch(f"/sources/{sid}", json={"enabled": True}, headers=world.admin), "PATCH", "/sources/{source_id}"),
    ]
    world.server.serve(PRIVATE_URL, catalog(entry("lrclib", DIGEST_B, permissions={"egress": ["x.example:443"], "limits": {"memory_mb": 64}})))
    checks += [
        (world.client.get(f"/sources/{sid}/catalog", headers=world.admin), "GET", "/sources/{source_id}/catalog"),
        (world.client.get("/sources/providers/installed", headers=world.admin), "GET", "/sources/providers/installed"),
        (world.client.post("/sources/providers/lrclib/update", json={"digest": DIGEST_B}, headers=world.admin), "POST", "/sources/providers/{name}/update"),
        (world.client.post("/sources/providers/lrclib/update", json={"digest": DIGEST_B}, headers=world.admin), "POST", "/sources/providers/{name}/update"),
        (world.client.delete("/sources/providers/lrclib", headers=world.admin), "DELETE", "/sources/providers/{name}"),
        (world.client.delete("/sources/providers/lrclib", headers=world.admin), "DELETE", "/sources/providers/{name}"),
        (world.client.delete("/sources/nope", headers=world.admin), "DELETE", "/sources/{source_id}"),
        (world.client.delete(f"/sources/{sid}", headers=world.admin), "DELETE", "/sources/{source_id}"),
    ]
    world.gateway.close()
    checks.append((world.client.get("/sources/providers/gateway", headers=world.admin), "GET", "/sources/providers/gateway"))

    for response, method, path in checks:
        assert_conforms(response, method, path)
