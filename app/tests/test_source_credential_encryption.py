"""
The token of a private source (``provider_sources.credential``) is stored sealed (docs/credentials.md),
reaches the catalog's server and the gateway as the plain token, never leaves in an API view, and when
the configured keys cannot open it the source simply has no token.
"""
from __future__ import annotations

import json
import logging

import pytest
import sqlalchemy as sa
from sqlmodel import Session

from core import secret_box
from db.models import InstalledProvider, ProviderSource
from services.gateway_client import GatewayClient
from services.gateway_sync import GatewaySync, desired_entries
from services.sources import CatalogFetcher, SourceManager, installed_view, source_view

from tests.credentials_support import KEY, THIRD_KEY, raw, raw_scalar, use_keys
from tests.sources_support import DIGEST_A, TOKEN, FakeCatalogServer, FakeControlSocket, catalog, entry

URL = "https://private.example/catalog.json"


@pytest.fixture
def world(engine):
    server, gateway = FakeCatalogServer(), FakeControlSocket()
    session = Session(engine)

    def manager() -> SourceManager:
        return SourceManager(
            session, announce_change=lambda: None,
            gateway=GatewaySync(GatewayClient(gateway.path), lambda: Session(engine)),
            fetcher=CatalogFetcher(server.transport),
        )

    world = type("World", (), {})()
    world.server, world.manager, world.session, world.engine, world.gateway = server, manager, session, engine, gateway
    yield world
    session.close()
    gateway.close()


def _add_private_source(world) -> ProviderSource:
    world.server.serve(URL, catalog(entry()))
    world.server.require_token[URL] = TOKEN
    return world.manager().add_source(URL, TOKEN)


def _stored(world) -> str:
    return raw_scalar(world.engine, "SELECT credential FROM provider_sources")


def test_the_token_of_a_source_is_stored_sealed_and_used_as_it_is(world):
    source = _add_private_source(world)

    stored = _stored(world)
    assert secret_box.is_sealed(stored) and TOKEN not in stored
    assert source.credential == TOKEN
    assert world.session.get(ProviderSource, source.id).credential == TOKEN
    assert world.manager().fetch_catalog(source).name == "Test catalog", "the catalog's server received the token"


def test_the_gateway_is_given_the_plain_token_in_memory_only(world):
    source = _add_private_source(world)
    world.session.add(InstalledProvider(
        source_id=source.id, name="lrclib", image="ghcr.io/o/lrclib", digest=DIGEST_A, provider_api_version=6, label="lrclib",
        roles=json.dumps(["lyrics"]), permissions=json.dumps({}),
    ))
    world.session.commit()

    (desired,) = desired_entries(world.session)

    assert desired["credential"] == TOKEN
    assert TOKEN not in json.dumps([source_view(source), installed_view(world.session.query(InstalledProvider).one(), source)])


def test_a_source_without_a_token_stores_nothing(world):
    world.server.serve(URL, catalog(entry()))

    source = world.manager().add_source(URL)

    assert source.credential is None
    assert raw(world.engine, "SELECT credential FROM provider_sources") == [(None,)]


def test_the_token_can_be_replaced_and_removed(world):
    source = _add_private_source(world)
    first = _stored(world)

    world.manager().edit_source(source.id, credential="canary-second-token")
    assert world.session.get(ProviderSource, source.id).credential == "canary-second-token"
    assert _stored(world) != first and "canary-second-token" not in _stored(world)

    world.manager().edit_source(source.id, credential=None)
    assert raw(world.engine, "SELECT credential FROM provider_sources") == [(None,)]


def test_editing_something_else_does_not_rewrite_the_token(world):
    source = _add_private_source(world)
    before = _stored(world)

    world.manager().edit_source(source.id, enabled=False)
    world.manager().edit_source(source.id, credential=TOKEN)  # the same token again

    assert _stored(world) == before


# ---------------------------------------------------------------------------
# A token that cannot be opened: the source has none
# ---------------------------------------------------------------------------


def test_with_the_wrong_key_the_source_has_no_token_and_nothing_is_overwritten(world, monkeypatch, caplog):
    source = _add_private_source(world)
    world.session.add(InstalledProvider(
        source_id=source.id, name="lrclib", image="ghcr.io/o/lrclib", digest=DIGEST_A, provider_api_version=6, label="lrclib",
        roles=json.dumps(["lyrics"]), permissions=json.dumps({}),
    ))
    world.session.commit()
    before = _stored(world)
    use_keys(monkeypatch, THIRD_KEY)
    caplog.set_level(logging.INFO)
    world.session.expire_all()

    reloaded = world.session.get(ProviderSource, source.id)
    assert reloaded.credential is None
    assert source_view(reloaded)["has_credential"] is False
    (desired,) = desired_entries(world.session)
    assert "credential" not in desired, "the gateway is not given what cannot be read"

    # An edit that does not name the token leaves the sealed text where it is (so the key can come back).
    world.manager().edit_source(source.id, enabled=False)
    assert _stored(world) == before
    assert TOKEN not in caplog.text and "MUSIZEER_CREDENTIALS_KEY" in caplog.text

    use_keys(monkeypatch, KEY)
    world.session.expire_all()
    assert world.session.get(ProviderSource, source.id).credential == TOKEN


def test_removing_a_token_that_cannot_be_read_removes_it(world, monkeypatch):
    source = _add_private_source(world)
    use_keys(monkeypatch, THIRD_KEY)
    world.session.expire_all()

    world.manager().edit_source(source.id, credential=None)

    assert raw(world.engine, "SELECT credential FROM provider_sources") == [(None,)]


def test_a_token_in_clear_in_the_table_is_not_used(world, caplog):
    caplog.set_level(logging.WARNING)
    source = _add_private_source(world)
    with world.engine.begin() as connection:
        connection.execute(sa.text("UPDATE provider_sources SET credential = :t"), {"t": "canary-in-clear"})
    world.session.expire_all()

    assert world.session.get(ProviderSource, source.id).credential is None
    assert "canary-in-clear" not in caplog.text




def test_a_token_cannot_be_given_to_the_constructor_where_it_would_silently_be_dropped():
    with pytest.raises(TypeError, match="set_credential"):
        ProviderSource(name="x", url="https://x.example/c.json", credential=TOKEN)

    source = ProviderSource(name="x", url="https://x.example/c.json")
    source.set_credential(TOKEN)
    assert source.credential == TOKEN and secret_box.is_sealed(source.sealed_credential)
