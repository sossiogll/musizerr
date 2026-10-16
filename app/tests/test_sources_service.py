"""
Sources and catalogs: adding a source (and what the catalog's server sees), installing with the
digest the admin was shown, updating with the difference in permissions, and telling the gateway.
The catalog's server and the gateway's control socket are fakes; the database is the one under test.
"""
from __future__ import annotations

import json
import logging

import pytest
from sqlmodel import Session, select

from db.models import InstalledProvider, ProviderSource
from services.gateway_client import GatewayClient
from services.gateway_sync import GatewaySync, desired_entries, generation
from services.sources import CatalogFetcher, SourceError, SourceManager, source_view
from tests.sources_support import (
    DIGEST_A,
    DIGEST_B,
    DIGEST_C,
    LRCLIB_PERMISSIONS,
    TOKEN,
    FakeCatalogServer,
    FakeControlSocket,
    catalog,
    entry,
)

URL = "https://catalog.example/registry.json"
PRIVATE_URL = "https://raw.githubusercontent.example/owner/private/main/catalog.json"


@pytest.fixture
def world(engine):
    server, gateway = FakeCatalogServer(), FakeControlSocket()
    announcements: list[str] = []
    session = Session(engine)

    def manager(s: Session | None = None) -> SourceManager:
        return SourceManager(
            s or session,
            announce_change=lambda: announcements.append("changed"),
            gateway=GatewaySync(GatewayClient(gateway.path), lambda: Session(engine)),
            fetcher=CatalogFetcher(server.transport),
        )

    world = type("World", (), {})()
    world.server, world.gateway, world.announcements, world.manager, world.session, world.engine = (
        server, gateway, announcements, manager, session, engine,
    )
    yield world
    session.close()
    gateway.close()


def added(world, url=URL, document=None, **kwargs) -> ProviderSource:
    world.server.serve(url, document or catalog(entry()))
    return world.manager().add_source(url, **kwargs)


# ---- adding a source ---------------------------------------------------------------------


def test_adding_a_source_fetches_and_validates_its_catalog(world):
    source = added(world, document=catalog(entry(), name="Official"))

    assert (source.name, source.url, source.enabled) == ("Official", URL, True)
    assert source.credential is None and source.allow_insecure_registry is False
    assert source_view(source)["has_credential"] is False
    assert world.gateway.pushes() == [], "nothing is installed: the gateway is not told anything"


def test_a_source_cannot_be_added_twice(world):
    added(world)

    with pytest.raises(SourceError) as refusal:
        world.manager().add_source(URL)

    assert (refusal.value.code, refusal.value.status) == ("source_exists", 409)


@pytest.mark.parametrize("url", ["", "   ", "ftp://x/y.json", "not a url", "https://", "https://user:pw@host/c.json", "http://h:99999/x", "x" * 3000])
def test_a_url_that_is_not_a_catalog_address_is_refused(world, url):
    with pytest.raises(SourceError) as refusal:
        world.manager().add_source(url)

    assert refusal.value.code == "invalid_source_url" and refusal.value.status == 422
    assert world.server.requests == []


@pytest.mark.parametrize("document, code", [
    (b"<html>", "catalog_invalid"),
    (b"[1, 2]", "catalog_invalid"),
    ({"name": "x", "providers": []}, "catalog_invalid"),
    ({"name": "x", "version": 1, "providers": []}, "catalog_invalid"),
    (b"x" * (2 * 1024 * 1024 + 1), "catalog_invalid"),
])
def test_a_document_that_is_not_a_catalog_is_refused_and_nothing_is_stored(world, document, code):
    world.server.serve(URL, document)

    with pytest.raises(SourceError) as refusal:
        world.manager().add_source(URL)

    assert refusal.value.code == code and refusal.value.status == 422
    assert world.manager().list_sources() == []


@pytest.mark.parametrize("status, code", [(401, "catalog_unauthorized"), (403, "catalog_unauthorized"), (404, "catalog_not_found"), (500, "catalog_unreachable")])
def test_a_server_that_does_not_give_the_catalog_is_told_apart(world, status, code):
    world.server.serve(URL, b"", status=status)

    with pytest.raises(SourceError) as refusal:
        world.manager().add_source(URL)

    assert refusal.value.code == code and refusal.value.status == 502


def test_a_server_that_cannot_be_reached_is_unreachable(world):
    with pytest.raises(SourceError) as refusal:
        world.manager().add_source("https://nowhere.example/catalog.json")  # the fake serves 404 for it

    assert refusal.value.code == "catalog_not_found"
    import httpx

    def refuse(request):
        raise httpx.ConnectError("boom")

    fetcher = CatalogFetcher(httpx.MockTransport(refuse))
    with pytest.raises(SourceError) as unreachable:
        fetcher.fetch(URL)
    assert unreachable.value.code == "catalog_unreachable" and "boom" not in str(unreachable.value)


# ---- the credential ------------------------------------------------------------------------


def test_the_credential_is_sent_as_a_bearer_token_to_the_catalogs_host_only(world):
    world.server.require_token[PRIVATE_URL] = TOKEN
    world.server.serve(PRIVATE_URL, catalog(entry()))

    source = world.manager().add_source(PRIVATE_URL, credential=f"  {TOKEN}\n")

    assert source.credential == TOKEN
    assert world.server.authorizations() == [(PRIVATE_URL, f"Bearer {TOKEN}")]


def test_the_credential_is_never_returned_logged_or_in_an_error(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.server.require_token[PRIVATE_URL] = "another-token"
    world.server.serve(PRIVATE_URL, catalog(entry()))

    with pytest.raises(SourceError) as refusal:
        world.manager().add_source(PRIVATE_URL, credential=TOKEN)
    assert TOKEN not in str(refusal.value) and refusal.value.code == "catalog_not_found"
    assert "cannot read it" in str(refusal.value)

    world.server.require_token[PRIVATE_URL] = TOKEN
    source = world.manager().add_source(PRIVATE_URL, credential=TOKEN)
    assert TOKEN not in json.dumps(source_view(source)) and TOKEN not in repr(source)
    assert TOKEN not in caplog.text


def test_a_token_goes_with_an_https_catalog_only(world):
    world.server.serve("http://plain.example/c.json", catalog(entry()))

    with pytest.raises(SourceError) as refusal:
        world.manager().add_source("http://plain.example/c.json", credential=TOKEN)

    assert refusal.value.code == "credential_requires_https"
    assert world.server.requests == []


def test_a_source_cannot_have_a_token_and_an_insecure_registry(world):
    with pytest.raises(SourceError) as refusal:
        world.manager().add_source(PRIVATE_URL, credential=TOKEN, allow_insecure_registry=True)

    assert refusal.value.code == "credential_with_insecure_registry" and refusal.value.status == 422


@pytest.mark.parametrize("credential", ["line\nbreak", "tab\there", "x" * 5000, 5])
def test_a_token_that_is_not_printable_text_is_refused(world, credential):
    with pytest.raises(SourceError) as refusal:
        world.manager().add_source(URL, credential=credential)

    assert refusal.value.code == "invalid_credential"


def test_an_empty_token_is_no_token(world):
    assert added(world, credential="   ").credential is None


def test_the_token_does_not_follow_a_redirect_to_another_host(world):
    world.server.require_token[PRIVATE_URL] = TOKEN
    world.server.redirect(PRIVATE_URL, "https://cdn.other.example/catalog.json")
    world.server.serve("https://cdn.other.example/catalog.json", catalog(entry()))
    world.server.require_token[PRIVATE_URL] = TOKEN

    world.manager().add_source(PRIVATE_URL, credential=TOKEN)

    other = [auth for url, auth in world.server.authorizations() if url.startswith("https://cdn.other.example")]
    assert other == [None], "the other host got no Authorization header"


def test_a_redirect_to_plain_http_is_refused_when_there_is_a_token(world):
    world.server.redirect(PRIVATE_URL, "http://plain.example/catalog.json")

    with pytest.raises(SourceError) as refusal:
        world.manager().add_source(PRIVATE_URL, credential=TOKEN)

    assert refusal.value.code == "catalog_unreachable"
    assert not any(url.startswith("http://plain.example") for url, _ in world.server.authorizations())


# ---- editing / toggling / removing --------------------------------------------------------


def test_a_source_is_enabled_disabled_and_its_token_replaced_or_removed(world):
    source = added(world)

    assert world.manager().toggle_source(source.id, False).enabled is False
    changed = world.manager().edit_source(source.id, credential=TOKEN, enabled=True)
    assert changed.credential == TOKEN and changed.enabled is True
    assert world.manager().edit_source(source.id, allow_insecure_registry=False).credential == TOKEN, "left alone"
    assert world.manager().edit_source(source.id, credential=None).credential is None


def test_the_flags_of_an_existing_source_obey_the_same_rules(world):
    source = added(world, url="http://dev.example/c.json")

    with pytest.raises(SourceError) as refusal:
        world.manager().edit_source(source.id, credential=TOKEN)
    assert refusal.value.code == "credential_requires_https"

    secure = added(world, url=PRIVATE_URL, credential=TOKEN)
    with pytest.raises(SourceError) as both:
        world.manager().edit_source(secure.id, allow_insecure_registry=True)
    assert both.value.code == "credential_with_insecure_registry"


def test_an_unknown_source_is_a_404(world):
    for call in (lambda m: m.toggle_source("nope", True), lambda m: m.remove_source("nope"), lambda m: m.browse("nope")):
        with pytest.raises(SourceError) as refusal:
            call(world.manager())
        assert (refusal.value.code, refusal.value.status) == ("source_not_found", 404)


def test_a_source_is_found_by_id_url_or_name(world):
    source = added(world, document=catalog(entry(), name="Official"))
    manager = world.manager()

    assert {manager.resolve_source(ref).id for ref in (source.id, URL, "Official")} == {source.id}
    with pytest.raises(SourceError, match="no source"):
        manager.resolve_source("other")


# ---- browsing -------------------------------------------------------------------------------


def test_browsing_a_catalog_says_what_can_be_installed_and_what_is_installed(world):
    source = added(world, document=catalog(
        entry("lrclib"), entry("old", provider_api_version=1), entry("broken", digest="main"),
    ))
    world.manager().install(source.id, "lrclib", DIGEST_A)

    page = world.manager().browse(source.id)

    by_name = {e["name"]: e for e in page["entries"]}
    assert page["name"] == "Test catalog" and page["source"]["id"] == source.id
    assert by_name["lrclib"]["installed"] and by_name["lrclib"]["installed_from_this_source"] and by_name["lrclib"]["update"] is None
    assert by_name["lrclib"]["compatible"] and by_name["lrclib"]["registry"] == "ghcr.io"
    assert by_name["old"]["compatible"] is False and "provider API version 1" in by_name["old"]["incompatibility"]
    assert [r["name"] for r in page["rejected"]] == ["broken"] and "digest" in page["rejected"][0]["reason"]


def test_a_disabled_source_cannot_be_browsed(world):
    source = added(world)
    world.manager().toggle_source(source.id, False)

    with pytest.raises(SourceError) as refusal:
        world.manager().browse(source.id)

    assert (refusal.value.code, refusal.value.status) == ("source_disabled", 409)


# ---- installing ---------------------------------------------------------------------------


def test_installing_records_the_image_by_digest_with_what_was_approved_and_tells_the_gateway(world):
    source = added(world)

    row = world.manager().install(source.id, "lrclib", DIGEST_A, approved_by="admin-1")

    assert (row.name, row.image, row.digest, row.label, row.approved_by) == ("lrclib", "ghcr.io/owner/musizerr-provider-lrclib", DIGEST_A, "LRCLIB", "admin-1")
    assert json.loads(row.roles) == ["lyrics"] and json.loads(row.permissions)["egress"] == ["lrclib.net:443"]
    assert row.approved_at is not None and row.approved_at.tzinfo is not None
    (push,) = world.gateway.pushes()
    assert push["providers"] == [{
        "name": "lrclib", "image": "ghcr.io/owner/musizerr-provider-lrclib", "digest": DIGEST_A, "roles": ["lyrics"],
        "permissions": {**LRCLIB_PERMISSIONS, "tcp_from_settings": [], "limits": {"memory_mb": None, "open_files": None, "processes": None}}, "insecure": False,
    }]
    assert push["generation"] == generation(push["providers"])
    assert world.announcements == ["changed"]


def test_the_sources_credential_and_registry_mode_travel_with_its_providers(world):
    world.server.require_token[PRIVATE_URL] = TOKEN
    private = added(world, url=PRIVATE_URL, credential=TOKEN)
    local = added(world, url="http://dev.example/c.json", document=catalog(entry("local")), allow_insecure_registry=True)
    world.manager().install(private.id, "lrclib", DIGEST_A)
    world.manager().install(local.id, "local", DIGEST_A)

    by_name = {p["name"]: p for p in world.gateway.pushes()[-1]["providers"]}

    assert by_name["lrclib"]["credential"] == TOKEN and by_name["lrclib"]["insecure"] is False
    assert "credential" not in by_name["local"] and by_name["local"]["insecure"] is True
    assert generation(world.gateway.pushes()[-1]["providers"]) == world.gateway.desired
    assert TOKEN not in json.dumps(world.manager().list_installed(check_updates=False))


def test_the_digest_must_be_the_one_the_admin_was_shown(world):
    source = added(world)

    with pytest.raises(SourceError) as refusal:
        world.manager().install(source.id, "lrclib", DIGEST_B)

    assert (refusal.value.code, refusal.value.status) == ("catalog_changed", 409)
    assert world.session.exec(select(InstalledProvider)).all() == [] and world.gateway.pushes() == []


@pytest.mark.parametrize("digest, code, status", [("nope", "catalog_changed", 409)])
def test_a_digest_that_is_not_one_is_refused_too(world, digest, code, status):
    source = added(world)

    with pytest.raises(SourceError) as refusal:
        world.manager().install(source.id, "lrclib", digest)

    assert (refusal.value.code, refusal.value.status) == (code, status)


def test_an_incompatible_entry_is_not_installable(world):
    source = added(world, document=catalog(entry("old", provider_api_version=1)))

    with pytest.raises(SourceError) as refusal:
        world.manager().install(source.id, "old", DIGEST_A)

    assert (refusal.value.code, refusal.value.status) == ("incompatible_provider", 422)


def test_a_provider_is_installed_once_and_must_be_in_the_catalog(world):
    source = added(world)
    world.manager().install(source.id, "lrclib", DIGEST_A)

    with pytest.raises(SourceError) as twice:
        world.manager().install(source.id, "lrclib", DIGEST_A)
    with pytest.raises(SourceError) as absent:
        world.manager().install(source.id, "ghost", DIGEST_A)

    assert twice.value.code == "already_installed" and absent.value.code == "provider_not_in_catalog" and absent.value.status == 404


def test_the_same_name_from_another_source_is_already_installed(world):
    first = added(world)
    second = added(world, url="https://other.example/c.json")
    world.manager().install(first.id, "lrclib", DIGEST_A)

    with pytest.raises(SourceError) as refusal:
        world.manager().install(second.id, "lrclib", DIGEST_A)

    assert refusal.value.code == "already_installed"


def test_a_gateway_that_does_not_answer_does_not_undo_an_install(world):
    source = added(world)
    world.gateway.close()

    row = world.manager().install(source.id, "lrclib", DIGEST_A)

    assert world.session.get(InstalledProvider, row.id) is not None
    assert world.announcements == ["changed"], "the other processes are still told"


# ---- updating -------------------------------------------------------------------------------


def _catalog_with_lrclib(digest, **kwargs):
    return catalog(entry("lrclib", digest, **kwargs))


def test_an_update_shows_the_difference_in_permissions_and_is_applied_with_the_approved_digest(world):
    source = added(world)
    world.manager().install(source.id, "lrclib", DIGEST_A)
    wider = {**LRCLIB_PERMISSIONS, "egress": ["lrclib.net:443", "evil.example:443"], "limits": {"memory_mb": 512}}
    world.server.serve(URL, _catalog_with_lrclib(DIGEST_B, permissions=wider))

    (installed,) = world.manager().list_installed()
    update = installed["update"]
    assert update["digest"] == DIGEST_B
    assert update["diff"]["egress"] == {"added": ["evil.example:443"], "removed": []}
    assert update["diff"]["limits"] == [{"name": "memory_mb", "old": None, "new": 512}]
    assert update["diff"]["grants_more"] is True and installed["update_error"] is None
    assert world.manager().browse(source.id)["entries"][0]["update"]["digest"] == DIGEST_B

    updated = world.manager().update("lrclib", DIGEST_B, approved_by="admin-2")

    assert updated.digest == DIGEST_B and updated.approved_by == "admin-2"
    assert json.loads(updated.permissions)["egress"] == ["evil.example:443", "lrclib.net:443"]
    last = world.gateway.pushes()[-1]["providers"][0]
    assert last["digest"] == DIGEST_B and "evil.example:443" in last["permissions"]["egress"]
    assert world.manager().list_installed()[0]["update"] is None


def test_an_update_to_another_digest_than_the_one_shown_is_refused(world):
    source = added(world)
    world.manager().install(source.id, "lrclib", DIGEST_A)
    world.server.serve(URL, _catalog_with_lrclib(DIGEST_C))

    with pytest.raises(SourceError) as refusal:
        world.manager().update("lrclib", DIGEST_B)

    assert refusal.value.code == "catalog_changed"
    assert world.session.exec(select(InstalledProvider)).one().digest == DIGEST_A


def test_there_is_nothing_to_update_when_the_digest_is_the_same(world):
    source = added(world)
    world.manager().install(source.id, "lrclib", DIGEST_A)

    with pytest.raises(SourceError) as refusal:
        world.manager().update("lrclib", DIGEST_A)

    assert (refusal.value.code, refusal.value.status) == ("already_current", 409)


def test_an_update_to_an_incompatible_version_is_listed_but_not_applied(world):
    source = added(world)
    world.manager().install(source.id, "lrclib", DIGEST_A)
    world.server.serve(URL, _catalog_with_lrclib(DIGEST_B, provider_api_version=99))

    (installed,) = world.manager().list_installed()
    with pytest.raises(SourceError) as refusal:
        world.manager().update("lrclib", DIGEST_B)

    assert installed["update"]["compatible"] is False
    assert refusal.value.code == "incompatible_provider"


def test_the_installed_list_says_why_an_update_could_not_be_looked_at(world):
    source = added(world)
    world.manager().install(source.id, "lrclib", DIGEST_A)

    world.server.serve(URL, b"", status=500)
    assert world.manager().list_installed()[0]["update_error"] == "catalog_unreachable"
    world.server.serve(URL, catalog())
    assert world.manager().list_installed()[0]["update_error"] == "provider_not_in_catalog"
    world.manager().toggle_source(source.id, False)
    assert world.manager().list_installed()[0]["update_error"] == "source_disabled"
    count = len(world.server.requests)
    assert world.manager().list_installed(check_updates=False)[0]["update"] is None
    assert len(world.server.requests) == count, "no catalog is fetched when updates are not asked for"


def test_a_catalog_is_fetched_once_for_all_the_providers_of_a_source(world):
    source = added(world, document=catalog(entry("one"), entry("two")))
    world.manager().install(source.id, "one", DIGEST_A)
    world.manager().install(source.id, "two", DIGEST_A)
    count = len(world.server.requests)

    world.manager().list_installed()

    assert len(world.server.requests) == count + 1


# ---- uninstalling / removing a source ---------------------------------------------------------


def test_uninstalling_removes_the_row_and_tells_the_gateway(world):
    source = added(world)
    world.manager().install(source.id, "lrclib", DIGEST_A)

    world.manager().uninstall("lrclib")

    assert world.session.exec(select(InstalledProvider)).all() == []
    assert world.gateway.pushes()[-1]["providers"] == [] and len(world.announcements) == 2
    with pytest.raises(SourceError) as refusal:
        world.manager().uninstall("lrclib")
    assert (refusal.value.code, refusal.value.status) == ("not_installed", 404)


def test_removing_a_source_uninstalls_its_providers_only(world):
    first = added(world, document=catalog(entry("one")))
    second = added(world, url="https://other.example/c.json", document=catalog(entry("two")))
    world.manager().install(first.id, "one", DIGEST_A)
    world.manager().install(second.id, "two", DIGEST_A)

    assert world.manager().remove_source(first.id) == ["one"]

    assert [p.name for p in world.session.exec(select(InstalledProvider)).all()] == ["two"]
    assert [p["name"] for p in world.gateway.pushes()[-1]["providers"]] == ["two"]
    assert [s.id for s in world.manager().list_sources()] == [second.id]


def test_changing_the_token_of_a_source_tells_the_gateway_but_toggling_does_not(world):
    world.server.require_token[PRIVATE_URL] = TOKEN
    source = added(world, url=PRIVATE_URL, credential=TOKEN)
    world.manager().install(source.id, "lrclib", DIGEST_A)
    pushes = len(world.gateway.pushes())

    world.manager().toggle_source(source.id, False)
    assert len(world.gateway.pushes()) == pushes
    world.manager().edit_source(source.id, credential="ghp_new_token_value")
    assert len(world.gateway.pushes()) == pushes + 1
    assert world.gateway.pushes()[-1]["providers"][0]["credential"] == "ghp_new_token_value"


# ---- the gateway's set follows the database ------------------------------------------------------


def test_a_restarted_gateway_is_told_the_set_again(world):
    source = added(world)
    world.manager().install(source.id, "lrclib", DIGEST_A)
    sync = GatewaySync(GatewayClient(world.gateway.path), lambda: Session(world.engine))
    listing = sync.client.listing()

    assert sync.ensure(listing) is False, "it has the set"
    world.gateway.restart()
    pushes = len(world.gateway.pushes())

    assert sync.ensure(sync.client.listing()) is True
    assert len(world.gateway.pushes()) == pushes + 1
    assert world.gateway.providers and sync.ensure(sync.client.listing()) is False


def test_the_generation_does_not_depend_on_the_credential(world):
    world.server.require_token[PRIVATE_URL] = TOKEN
    source = added(world, url=PRIVATE_URL, credential=TOKEN)
    world.manager().install(source.id, "lrclib", DIGEST_A)
    before = generation(desired_entries(world.session))

    world.manager().edit_source(source.id, credential="ghp_another_token_value")

    assert generation(desired_entries(world.session)) == before
    assert before == world.gateway.desired


def test_the_gateways_refusal_of_an_entry_is_reported_not_raised(world):
    source = added(world)
    world.gateway.install_reply = {"ok": True, "accepted": [], "rejected": {"lrclib": "digest is required"}}

    world.manager().install(source.id, "lrclib", DIGEST_A)
    result = GatewaySync(GatewayClient(world.gateway.path), lambda: Session(world.engine)).push()

    assert result.pushed and result.rejected == {"lrclib": "digest is required"}


def test_the_gateways_state_is_reported_for_the_admin(world):
    source = added(world)
    world.manager().install(source.id, "lrclib", DIGEST_A)
    world.gateway.providers[0].update(state="refused", reason="the image declares permissions that are not the ones approved")
    world.gateway.rejected = {"junk": "no digest"}

    state = world.manager().gateway_state({"other": "refused by the host"})

    assert state["reachable"] and state["status"] == "degraded" and state["sandbox"]["available"] is True
    assert state["providers"][0]["state"] == "refused" and "approved" in state["providers"][0]["reason"]
    assert state["rejected"] == {"junk": "no digest"} and state["host_rejected"] == {"other": "refused by the host"}
    world.gateway.sandbox = {"available": False, "reason": "no user namespaces"}
    assert world.manager().gateway_state()["status"] == "unavailable"


def test_an_unreachable_gateway_is_a_state_not_an_error(world):
    world.gateway.close()

    state = world.manager().gateway_state()

    assert state["reachable"] is False and state["status"] == "unreachable" and state["providers"] == []


def test_a_catalog_that_keeps_the_digest_but_declares_other_permissions_is_an_update_too(world):
    source = added(world)
    world.manager().install(source.id, "lrclib", DIGEST_A)
    broader = {**LRCLIB_PERMISSIONS, "egress": ["lrclib.net:443", "extra.example:443"]}
    world.server.serve(URL, _catalog_with_lrclib(DIGEST_A, permissions=broader))

    (installed,) = world.manager().list_installed()
    assert installed["update"]["digest"] == DIGEST_A
    assert installed["update"]["diff"]["egress"]["added"] == ["extra.example:443"]

    updated = world.manager().update("lrclib", DIGEST_A)

    assert "extra.example:443" in json.loads(updated.permissions)["egress"]
    assert "extra.example:443" in world.gateway.pushes()[-1]["providers"][0]["permissions"]["egress"]
    assert world.manager().list_installed()[0]["update"] is None
