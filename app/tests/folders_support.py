"""Shared by the tests of root folders and folder settings (ADR-0008): an instance with a fake gateway, a few roots, folder
values stored the way the settings service stores them, and storage providers that declare a library folder. Fakes only."""
from __future__ import annotations

from sqlmodel import Session

from musizeer_sdk.settings import ProviderSetting

from core.settings_resolver import namespace_provider_key
from db import root_store
from db.models import RootFolder
from db.settings_store import SettingsRepository
from tests.conftest import FakeStorageProvider


class FolderStorage(FakeStorageProvider):
    """A storage provider whose library is a folder: ``root_dir`` (a ``folder`` setting) declared with
    ``library_folder_setting()``."""

    def __init__(self, name: str = "foldered") -> None:
        super().__init__(name)

    def config_schema(self):
        return [ProviderSetting(key="root_dir", type="folder", label="Library folder", default="Music")]

    def library_folder_setting(self):
        return "root_dir"


class UndeclaredFolderStorage(FolderStorage):
    """A storage with one folder setting and no ``library_folder_setting()``: the folder is its only one."""

    def __init__(self, name: str = "undeclared") -> None:
        super().__init__(name)

    def library_folder_setting(self):
        return None


class RemoteStorage(FakeStorageProvider):
    """A storage whose library is not a folder of this machine: a plain string setting."""

    def __init__(self, name: str = "remote") -> None:
        super().__init__(name)

    def config_schema(self):
        return [ProviderSetting(key="remote_base_path", type="string", label="Remote path", default="/music")]


def add_root(engine, path: str, label: str | None = None, state: str = "accepted", reason: str = "") -> RootFolder:
    with Session(engine) as session:
        root = root_store.create(session, label or path.rsplit("/", 1)[-1].capitalize(), path)
        if state != "unchecked":
            root_store.record_verdicts(session, {root.id: (state, reason)})
            session.refresh(root)
        session.expunge(root)
        return root


def store_folder(engine, key: str, root: RootFolder, path: str = "") -> None:
    """A folder value in the settings store, as ``SettingsService.write_values`` leaves it."""
    with Session(engine) as session:
        SettingsRepository(session).set_system(key, {"root_id": root.id, "path": path})


def store_provider_folder(engine, provider: str, short_key: str, root: RootFolder, path: str = "") -> None:
    store_folder(engine, namespace_provider_key(provider, short_key), root, path)
