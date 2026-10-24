"""
The one-shot conversion of the folder values an older version stored (ADR-0008, "Migration"): the working folder set aside by
migration 0046 and the paths typed in the settings that providers now declare as folders become a root plus a path. It runs
when the gateway answers, is idempotent, never invents a default, and leaves unset (with the reason) whatever cannot be
expressed.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session

from db import root_store
from db.settings_store import SettingsRepository
from services import folders, roots
from services.config_manager import ConfigManager
from services.gateway_client import GatewayClient
from services.gateway_sync import GatewaySync
from services import provider_events
from tests.conftest import FakeRegistry
from tests.folders_support import FolderStorage, RemoteStorage, UndeclaredFolderStorage, add_root, store_folder
from tests.sources_support import FakeControlSocket

OLD_LEAF = ".legacy-hidden"
OLD_DEFAULT = "/downloads/" + OLD_LEAF
WORKING = "workspace_dir"
LIBRARY_KEY = "provider.foldered.root_dir"


@pytest.fixture
def gateway():
    fake = FakeControlSocket()
    yield fake
    fake.close()


@pytest.fixture
def client(gateway):
    return GatewayClient(gateway.path)


@pytest.fixture
def registry():
    return FakeRegistry(storage=[FolderStorage(), UndeclaredFolderStorage(), RemoteStorage()])


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def legacy(session, value=None):
    root_store.state_set(session, roots.LEGACY_KEY, {"value": value, "default": OLD_DEFAULT, "old_leaf": OLD_LEAF})


def stored(session, key):
    return SettingsRepository(session).get_system(key)


def all_roots(session):
    return {root.path: root for root in root_store.list_roots(session)}


def not_mounted(gateway, path, reason="it is part of the container's own file system, not a mounted volume"):
    gateway.inspections[path] = {
        "ok": True, "path": path, "exists": True, "directory": True, "writable": False, "mounted": False, "reason": reason,
    }


# ------------------------------------------------------------------
# The working folder
# ------------------------------------------------------------------


def test_the_old_default_becomes_the_root_of_its_parent_and_the_new_hidden_name(session, client, registry):
    legacy(session)

    report = roots.convert_legacy(session, client, registry)

    (root,) = all_roots(session).values()
    assert (root.path, root.label) == ("/downloads", "Downloads")
    assert stored(session, WORKING) == {"root_id": root.id, "path": ".musizeer-work"}
    assert report.converted == {WORKING: {"root_id": root.id, "path": ".musizeer-work"}} and report.unset == {}
    assert root_store.state_get(session, roots.LEGACY_KEY) is None, "the record is consumed"


def test_a_stored_value_wins_over_the_old_default_and_keeps_its_last_component(session, client, registry):
    legacy(session, "/data/work")

    roots.convert_legacy(session, client, registry)

    (root,) = all_roots(session).values()
    assert root.path == "/data" and stored(session, WORKING) == {"root_id": root.id, "path": "work"}


def test_a_stored_value_ending_in_the_old_hidden_name_gets_the_new_one(session, client, registry):
    legacy(session, "/media/staging/" + OLD_LEAF)

    roots.convert_legacy(session, client, registry)

    (root,) = all_roots(session).values()
    assert root.path == "/media/staging" and stored(session, WORKING)["path"] == ".musizeer-work"


def test_a_root_that_already_holds_the_folder_is_used_instead_of_making_another(session, client, registry, engine):
    media = add_root(engine, "/media")
    legacy(session, "/media/staging/" + OLD_LEAF)

    roots.convert_legacy(session, client, registry)

    assert list(all_roots(session)) == ["/media"], "no second root"
    assert stored(session, WORKING) == {"root_id": media.id, "path": "staging/.musizeer-work"}


def test_the_deepest_root_that_holds_it_is_the_one(session, client, registry, engine):
    add_root(engine, "/media")
    inner = add_root(engine, "/media/Musica-staging")
    legacy(session, "/media/Musica-staging/" + OLD_LEAF)

    roots.convert_legacy(session, client, registry)

    assert stored(session, WORKING) == {"root_id": inner.id, "path": ".musizeer-work"}


def test_a_folder_the_gateway_does_not_see_mounted_is_left_unset_with_the_reason(session, client, registry, gateway):
    not_mounted(gateway, "/downloads")
    legacy(session)

    report = roots.convert_legacy(session, client, registry)

    assert all_roots(session) == {} and stored(session, WORKING) is None
    assert "not a mounted volume" in report.unset[WORKING] and "could not be kept" in report.unset[WORKING]
    record = root_store.state_get(session, roots.CONVERSION_KEY)
    assert record["unset"][WORKING] == report.unset[WORKING] and record["converted"] == {}


def test_the_reason_is_what_the_warning_says_while_the_working_folder_is_not_set(session, client, registry, gateway, engine):
    not_mounted(gateway, "/downloads")
    legacy(session)
    roots.convert_legacy(session, client, registry)

    with pytest.raises(folders.FoldersNotReady) as blocked:
        folders.working_folder(ConfigManager(engine=engine))

    assert "not a mounted volume" in blocked.value.detail


def test_a_folder_the_gateway_says_is_not_writable_is_not_a_root(session, client, registry, gateway):
    gateway.inspections["/downloads"] = {
        "ok": True, "path": "/downloads", "exists": True, "directory": True, "writable": False, "mounted": True, "reason": "is not writable by the gateway",
    }
    legacy(session)

    report = roots.convert_legacy(session, client, registry)

    assert all_roots(session) == {} and "not writable" in report.unset[WORKING]


@pytest.mark.parametrize("value", ["/downloads", "relative/work", "/a/../b"])
def test_a_working_folder_that_cannot_be_expressed_is_left_unset(session, client, registry, value):
    legacy(session, value)

    report = roots.convert_legacy(session, client, registry)

    assert stored(session, WORKING) is None and list(report.unset) == [WORKING]


def test_a_working_folder_the_administrator_already_set_is_not_touched(session, client, registry, engine):
    chosen = add_root(engine, "/elsewhere")
    store_folder(engine, WORKING, chosen, "mine")
    legacy(session)

    report = roots.convert_legacy(session, client, registry)

    assert stored(session, WORKING) == {"root_id": chosen.id, "path": "mine"} and list(all_roots(session)) == ["/elsewhere"]
    assert not report.converted and root_store.state_get(session, roots.LEGACY_KEY) is None


# ------------------------------------------------------------------
# A provider's folder
# ------------------------------------------------------------------


def put(session, key, value):
    SettingsRepository(session).set_system(key, value)


def test_a_typed_library_path_becomes_a_root_at_the_path_itself(session, client, registry):
    put(session, LIBRARY_KEY, "/library")

    report = roots.convert_legacy(session, client, registry)

    (root,) = all_roots(session).values()
    assert (root.path, root.label) == ("/library", "Library")
    assert stored(session, LIBRARY_KEY) == {"root_id": root.id, "path": ""} and report.converted[LIBRARY_KEY]["path"] == ""


def test_a_library_inside_a_declared_root_becomes_that_root_plus_the_rest(session, client, registry, engine):
    media = add_root(engine, "/media")
    put(session, LIBRARY_KEY, "/media/Musica/")

    roots.convert_legacy(session, client, registry)

    assert list(all_roots(session)) == ["/media"] and stored(session, LIBRARY_KEY) == {"root_id": media.id, "path": "Musica"}


def test_the_old_default_library_inside_the_working_folders_root_is_the_working_folder_inside_the_library(session, client, registry):
    """ADR-0008 decision 8: an install converted from the old defaults ends with the working folder inside the library."""
    legacy(session)
    put(session, LIBRARY_KEY, "/downloads")

    roots.convert_legacy(session, client, registry)

    (root,) = all_roots(session).values()
    assert stored(session, LIBRARY_KEY) == {"root_id": root.id, "path": ""}
    assert stored(session, WORKING) == {"root_id": root.id, "path": ".musizeer-work"}


def test_a_library_path_the_gateway_does_not_see_mounted_is_cleared_and_explained(session, client, registry, gateway):
    not_mounted(gateway, "/music")
    put(session, LIBRARY_KEY, "/music")

    report = roots.convert_legacy(session, client, registry)

    assert stored(session, LIBRARY_KEY) is None and all_roots(session) == {}
    assert "not a mounted volume" in report.unset[LIBRARY_KEY]


@pytest.mark.parametrize("value", ["relative", "/a/../b", "/"])
def test_a_library_path_that_is_not_an_absolute_clean_path_is_cleared(session, client, registry, value):
    put(session, LIBRARY_KEY, value)

    report = roots.convert_legacy(session, client, registry)

    assert stored(session, LIBRARY_KEY) is None and LIBRARY_KEY in report.unset


def test_an_empty_text_is_simply_cleared(session, client, registry):
    put(session, LIBRARY_KEY, "  ")

    report = roots.convert_legacy(session, client, registry)

    assert stored(session, LIBRARY_KEY) is None and report.unset == {}


def test_a_provider_setting_that_is_not_a_folder_is_never_touched(session, client, registry):
    put(session, "provider.remote.remote_base_path", "/music")

    assert roots.convert_legacy(session, client, registry) is None
    assert stored(session, "provider.remote.remote_base_path") == "/music" and all_roots(session) == {}


def test_a_folder_value_already_converted_is_left_as_it_is(session, client, registry, engine):
    library = add_root(engine, "/library")
    store_folder(engine, LIBRARY_KEY, library, "Music")

    assert roots.convert_legacy(session, client, registry) is None
    assert stored(session, LIBRARY_KEY) == {"root_id": library.id, "path": "Music"}


# ------------------------------------------------------------------
# Idempotent, and it never invents anything
# ------------------------------------------------------------------


def test_a_fresh_install_has_nothing_to_convert_and_nothing_is_made(session, client, registry, gateway):
    assert roots.pending(session, registry) is False
    assert roots.convert_legacy(session, client, registry) is None

    assert all_roots(session) == {} and stored(session, WORKING) is None and gateway.requests == []
    assert root_store.state_get(session, roots.CONVERSION_KEY) is None


def test_a_provider_with_no_stored_folder_gets_no_default_not_even_the_old_one(session, client, registry):
    legacy(session)  # an upgraded install that never typed a library path

    roots.convert_legacy(session, client, registry)

    assert stored(session, LIBRARY_KEY) is None, "the library folder is the administrator's choice"


def test_running_it_again_changes_nothing(session, client, registry):
    legacy(session)
    put(session, LIBRARY_KEY, "/library")
    first = roots.convert_legacy(session, client, registry)
    before = (dict(all_roots(session)), stored(session, WORKING), stored(session, LIBRARY_KEY))

    second = roots.convert_legacy(session, client, registry)

    assert first.converted and second is None
    assert before == (dict(all_roots(session)), stored(session, WORKING), stored(session, LIBRARY_KEY))
    assert roots.pending(session, registry) is False


def test_the_record_keeps_what_was_converted_and_what_was_not(session, client, registry, gateway):
    not_mounted(gateway, "/music")
    legacy(session)
    put(session, LIBRARY_KEY, "/music")

    roots.convert_legacy(session, client, registry)

    record = root_store.state_get(session, roots.CONVERSION_KEY)
    assert set(record["converted"]) == {WORKING} and set(record["unset"]) == {LIBRARY_KEY} and record["done_at"]


def test_a_folder_the_administrator_sets_afterwards_works_whatever_the_note_says(session, client, registry, gateway, engine):
    not_mounted(gateway, "/downloads")
    legacy(session)
    roots.convert_legacy(session, client, registry)

    root = add_root(engine, "/downloads")
    store_folder(engine, WORKING, root, "work")

    assert str(folders.working_folder(ConfigManager(engine=engine))) == "/downloads/work"


def test_a_gateway_that_goes_away_midway_converts_nothing_and_loses_nothing(session, client, registry, gateway):
    legacy(session)
    gateway.close()

    assert roots.convert_legacy(session, client, registry) is None

    assert all_roots(session) == {} and root_store.state_get(session, roots.LEGACY_KEY) is not None


def test_the_settings_written_are_reported_for_the_cache_of_the_caller(session, client, registry):
    legacy(session)
    written = []

    roots.convert_legacy(session, client, registry, on_written=written.append)

    assert written and WORKING in written[0]


# ------------------------------------------------------------------
# When the gateway first answers
# ------------------------------------------------------------------


class Registry:
    gateway_key = None

    def __init__(self, inner):
        self.inner = inner

    def all_storage(self):
        return self.inner.all_storage()

    def all_metadata(self):
        return []

    def all_download(self):
        return []

    def all_lyrics(self):
        return []

    def all_sync(self):
        return []

    def all_identification(self):
        return []


def test_the_watcher_converts_when_the_gateway_answers_then_tells_it_the_roots_and_the_working_folder(engine, gateway, registry):
    with Session(engine) as session:
        legacy(session)
    client = GatewayClient(gateway.path)
    sync = GatewaySync(client, lambda: Session(engine))

    def convert():
        with Session(engine) as session:
            return roots.convert_legacy(session, client, registry)

    watcher = provider_events.GatewayWatcher(
        Registry(registry), client, lambda: None, sync=sync, workspace_dir=lambda: "/downloads/.musizeer-work", convert=convert,
    )

    watcher.check()

    with Session(engine) as session:
        (root,) = all_roots(session).values()
        assert root.state == "accepted", "the verdict of the push is recorded"
    push = gateway.pushes()[-1]
    assert push["roots"] == [{"id": root.id, "path": "/downloads"}] and push["workspace"] == "/downloads/.musizeer-work"


def test_the_watcher_survives_a_conversion_that_fails(engine, gateway, registry, caplog):
    client = GatewayClient(gateway.path)

    def explode():
        raise RuntimeError("database is busy")

    watcher = provider_events.GatewayWatcher(
        Registry(registry), client, lambda: None, workspace_dir=lambda: "/x", convert=explode,
    )

    assert watcher.check() in (True, False)
    assert "could not convert the folder values" in caplog.text
