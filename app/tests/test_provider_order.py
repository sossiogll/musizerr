"""
The order of the metadata providers: the pure rule (``core/provider_order.py``), the service that
reads it for a viewer (``services/provider_order.py``) and the readers that must agree on it
(``GET /providers/public``, ``GET /settings/values``, a detail route). No provider is real: the
generic fakes stand in, and "installation" is rows of ``installed_providers``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.provider_order import effective_order, in_installation_order
from db.engine import get_session
from db.models import InstalledProvider, ProviderSource, User
from db.settings_store import SettingsRepository
from routes import deps
from routes.config import router as config_router
from services import provider_order
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeMetadataProvider, FakeRegistry
from tests.database import new_engine
from tests.test_artist_top_tracks import _Artists, _t

KEY = provider_order.METADATA_PRIORITY_KEY
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---- the pure rule ----------------------------------------------------------------------


def test_an_empty_list_gives_every_provider_in_the_order_given():
    assert effective_order([], ["a", "b", "c"]) == ["a", "b", "c"]
    assert effective_order(None, ["a", "b", "c"]) == ["a", "b", "c"]


def test_a_partial_list_keeps_its_names_first_then_appends_the_others():
    assert effective_order(["c"], ["a", "b", "c"]) == ["c", "a", "b"]


def test_a_stale_name_and_a_repeated_one_are_dropped():
    assert effective_order(["gone", "b", "b", "a"], ["a", "b"]) == ["b", "a"]


def test_nothing_available_gives_nothing():
    assert effective_order(["a"], []) == []


def test_installation_order_sorts_by_position_and_puts_the_unknown_last():
    assert in_installation_order(["c", "x", "a", "a"], ["a", "b", "c"]) == ["a", "c", "x"]


# ---- the service ------------------------------------------------------------------------


def _install(session: Session, names: list[str], roles=("metadata",), first_day: int = 0) -> None:
    """Install ``names`` oldest first (a day apart, whatever their ids)."""
    source = session.get(ProviderSource, "src") or ProviderSource(id="src", name="s", url="https://catalog.example/c.json")
    session.add(source)
    session.flush()
    for index, name in enumerate(names):
        session.add(InstalledProvider(
            source_id=source.id, name=name, image=f"ghcr.io/o/{name}", digest="sha256:" + "a" * 64,
            provider_api_version=5, label=name, roles=json.dumps(list(roles)), permissions="{}",
            installed_at=T0 + timedelta(days=first_day + index),
        ))
    session.commit()


@pytest.fixture
def world():
    engine = new_engine()
    config = ConfigManager(engine=engine)
    with Session(engine) as session:
        yield type("World", (), {"engine": engine, "config": config, "session": session})()


def _user(session: Session, name: str) -> str:
    """A user row (the personal override has a foreign key to it)."""
    user = User(username=name, password_hash="x", role="user")
    session.add(user)
    session.commit()
    return user.id


def _registry(*names):
    return FakeRegistry(metadata=[FakeMetadataProvider(n) for n in names])


def test_an_empty_stored_list_gives_the_installation_order_not_the_registry_order(world):
    _install(world.session, ["old", "mid", "new"])

    order = provider_order.effective_metadata_priority(_registry("new", "old", "mid"), world.config, world.session)

    assert order == ["old", "mid", "new"]


def test_the_system_list_comes_first_then_the_rest_by_installation(world):
    _install(world.session, ["a", "b", "c"])
    world.config.update({KEY: ["c", "gone"]})

    assert provider_order.effective_metadata_priority(_registry("a", "b", "c"), world.config, world.session) == ["c", "a", "b"]


def test_a_provider_that_is_not_registered_is_left_out(world):
    _install(world.session, ["a", "b"])
    world.config.update({KEY: ["b", "a"]})

    assert provider_order.effective_metadata_priority(_registry("a"), world.config, world.session) == ["a"]


def test_a_user_override_goes_first_and_the_missing_ones_follow(world):
    _install(world.session, ["a", "b", "c"])
    world.config.update({KEY: ["c"]})
    alice, bob = _user(world.session, "alice"), _user(world.session, "bob")
    SettingsRepository(world.session).set_user(KEY, alice, ["b"])
    registry = _registry("a", "b", "c")

    assert provider_order.effective_metadata_priority(registry, world.config, world.session, alice) == ["b", "a", "c"]
    assert provider_order.effective_metadata_priority(registry, world.config, world.session, bob) == ["c", "a", "b"]
    assert provider_order.effective_metadata_priority(registry, world.config, world.session) == ["c", "a", "b"]


def test_an_empty_personal_list_is_no_override(world):
    _install(world.session, ["a", "b"])
    world.config.update({KEY: ["b"]})
    alice = _user(world.session, "alice")
    SettingsRepository(world.session).set_user(KEY, alice, [])

    assert provider_order.effective_metadata_priority(_registry("a", "b"), world.config, world.session, alice) == ["b", "a"]


def test_a_provider_without_an_installation_record_follows_the_installed_ones(world):
    _install(world.session, ["a"])

    assert provider_order.effective_metadata_priority(_registry("loose", "a"), world.config, world.session) == ["a", "loose"]


def test_syncing_the_default_appends_the_installed_providers_and_is_idempotent(world):
    _install(world.session, ["a", "b", "c"])
    SettingsRepository(world.session).set_system(KEY, ["b"])

    assert provider_order.sync_default(world.session) is True
    world.session.commit()
    assert SettingsRepository(world.session).get_system(KEY) == ["b", "a", "c"]
    assert provider_order.sync_default(world.session) is False


def test_syncing_an_empty_default_seeds_it_in_installation_order(world):
    _install(world.session, ["a", "b"])

    provider_order.sync_default(world.session)
    world.session.commit()

    assert SettingsRepository(world.session).get_system(KEY) == ["a", "b"]


def test_syncing_ignores_providers_without_the_metadata_role_and_drops_stale_names(world):
    _install(world.session, ["a"], roles=("metadata",))
    _install(world.session, ["lyr"], roles=("lyrics",), first_day=10)
    SettingsRepository(world.session).set_system(KEY, ["gone", "a"])

    provider_order.sync_default(world.session)
    world.session.commit()

    assert SettingsRepository(world.session).get_system(KEY) == ["a"]


def test_a_reinstalled_provider_goes_to_the_end(world):
    _install(world.session, ["a", "b"])
    SettingsRepository(world.session).set_system(KEY, ["a", "b"])

    provider_order.sync_default(world.session, reinstalled="a")
    world.session.commit()

    assert SettingsRepository(world.session).get_system(KEY) == ["b", "a"]


# ---- the readers agree ------------------------------------------------------------------


@pytest.fixture
def api(world):
    """Three metadata providers installed in the order pa, pc, pb; the registry lists them pa, pb, pc."""
    from routes.content.artist import router as artist_router

    _install(world.session, ["pa", "pc", "pb"])
    providers = [
        _Artists("pa", []),
        _Artists("pb", [_t("pb", "b1", "Larsen")]),
        _Artists("pc", [_t("pc", "c1", "Confusianesimo")]),
    ]
    registry = FakeRegistry(metadata=providers)
    world.config.update({"auth_enabled": False})

    app = FastAPI()
    app.include_router(config_router)
    app.include_router(artist_router)

    def _session():
        with Session(world.engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: world.config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    return TestClient(app)


def _top_ids(api):
    return [i["provider_id"] for i in api.get("/artist/pa/pa-art/top-tracks?limit=1").json()["items"]]


def test_the_public_list_the_settings_and_a_detail_route_use_the_same_order(api):
    # Stored value empty: the order is the installation one (pa, pc, pb), not the registry's.
    public = api.get("/providers/public").json()
    settings = api.get("/settings/values").json()[KEY]
    assert public["metadata_priority"] == public["metadata"] == settings == ["pa", "pc", "pb"]

    # pa has nothing: the top tracks fall back on the next provider IN THAT ORDER (pc, not pb).
    assert _top_ids(api) == ["c1"]


def test_no_reader_looks_at_the_stored_list_directly():
    """The stored list is read in ONE place; everything else asks ``services/provider_order``."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    allowed = {"services/provider_order.py", "core/provider_order.py", "core/settings_catalog.py", "db/models.py"}
    offenders = [
        str(path.relative_to(root))
        for folder in ("routes", "services", "workers", "core", "db", "scripts")
        for path in (root / folder).rglob("*.py")
        if str(path.relative_to(root)) not in allowed and "metadata_provider_priority" in path.read_text()
    ]
    assert offenders == []


def test_a_stored_partial_order_is_completed_the_same_way_everywhere(api, world):
    world.config.update({KEY: ["pb"]})

    public = api.get("/providers/public").json()
    assert public["metadata_priority"] == public["metadata"] == api.get("/settings/values").json()[KEY] == ["pb", "pa", "pc"]
    assert _top_ids(api) == ["b1"]
