"""A provider that runs behind the gateway and declares a folder setting (ADR-0008): the registry tells its context which
settings are folders, the call it makes carries the absolute path (never the root), and that path is what the gateway is
granted. The provider never learns that roots exist."""
from __future__ import annotations

import pytest

from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizeer_sdk.interfaces import PROVIDER_API_VERSION, StorageProvider
from musizeer_sdk.permissions import FilesystemAccess, Permissions
from musizeer_sdk.settings import ProviderSetting
from services.gateway_client import GatewayClient, GatewayProviderContext
from services.provider_registry import ProviderRegistry

from tests.folders_support import add_root
from tests.gateway_support import FakeGateway, declared, gateway  # noqa: F401 — `gateway` is a fixture

SESSION = AuthSession.anonymous()
PERMISSIONS = Permissions(filesystem_from_settings=(FilesystemAccess("root_dir", "rw"),))
SEEN: list = []


class Store(StorageProvider):
    name = "store"
    label = "Store"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    provider_api_version = PROVIDER_API_VERSION
    permissions = PERMISSIONS

    def config_schema(self):
        return [ProviderSetting(key="root_dir", type="folder", label="Library", default="Music"), ProviderSetting(key="other", type="string", label="Other", default="x")]

    def library_folder_setting(self):
        return "root_dir"

    def validate_config(self, config): return True
    def test_connection(self, session): return None
    def store(self, source, relative_dest, session): return relative_dest
    def stream(self, relative_path, workspace, session): raise NotImplementedError
    def delete(self, relative_path, session): return True

    def exists(self, relative_path, session):
        SEEN.append(dict(self.context.settings()))
        return True


@pytest.fixture
def world(gateway, engine, monkeypatch):  # noqa: F811
    SEEN.clear()
    monkeypatch.setattr("db.engine.engine", engine)
    library = add_root(engine, "/library")
    stored = {"root_dir": {"root_id": library.id, "path": "Music/Rock"}, "other": "value"}
    monkeypatch.setattr(GatewayProviderContext, "settings", lambda self: dict(stored))
    gateway.serve(Store, "store", declaration=declared("store", roles=("storage",), permissions=PERMISSIONS))
    gateway.stored, gateway.library = stored, library
    return gateway


def registry_for(gateway: FakeGateway) -> ProviderRegistry:
    return ProviderRegistry(gateway=GatewayClient(gateway.run_dir / "gateway.sock"))


def test_the_registry_tells_the_context_of_a_provider_which_of_its_settings_are_folders(world):
    provider = registry_for(world).get_storage_provider("store")

    assert provider.context._folder_keys == frozenset({"root_dir"})  # noqa: SLF001


def test_the_provider_receives_the_absolute_path_and_the_gateway_is_granted_the_same_one(world):
    provider = registry_for(world).get_storage_provider("store")

    assert provider.library_folder_setting() == "root_dir", "declared in the manifest, no call"
    assert provider.exists("a.flac", SESSION) is True

    assert SEEN == [{"root_dir": "/library/Music/Rock", "other": "value"}]
    grants = [r for r in world.requests if r["op"] == "egress"]
    assert grants[0]["paths"] == {"root_dir": "/library/Music/Rock"}
    assert world.library.id not in str(SEEN) + str(grants)


def test_a_folder_that_is_not_set_reaches_the_provider_as_no_value_and_grants_nothing(world, monkeypatch):
    monkeypatch.setattr(GatewayProviderContext, "settings", lambda self: {"other": "value"})
    provider = registry_for(world).get_storage_provider("store")

    provider.exists("a.flac", SESSION)

    assert SEEN == [{"other": "value"}]
    assert [r for r in world.requests if r["op"] == "egress"][0].get("paths") is None
