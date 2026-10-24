"""
Folder settings (ADR-0008): the value is ``{root_id, path}``, validated and resolved against the stored roots; the working
folder has no default; a provider's folder setting is resolved to the plain absolute path its context carries (the
provider never sees a root); the settings service refuses a folder whose root does not exist, that the gateway refuses, or
that leaves its root.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session

from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizeer_sdk.interfaces import MetadataProvider
from musizeer_sdk.permissions import FilesystemAccess, Permissions, path_values
from musizeer_sdk.settings import ProviderSetting

from core import folders as rules
from core.settings_catalog import CATALOG, get_def
from core.settings_resolver import SettingError, coerce_value, merged_schema, provider_setting_def, validate_value
from db import root_store
from db.models import User
from db.settings_store import SettingsRepository
from services import folders
from services.config_manager import ConfigManager
from services.gateway_client import GatewayClient
from services.provider_context import HostProviderContext
from services.settings import SettingsService, SettingsWriteError
from tests.conftest import FakeRegistry
from tests.folders_support import add_root
from tests.loopback import as_proxy


class FolderedProvider(MetadataProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    name = "foldery"
    label = "Foldery"
    permissions = Permissions(filesystem_from_settings=(FilesystemAccess("root_dir", "rw"),))

    def __init__(self):
        pass

    def config_schema(self):
        return [
            ProviderSetting(key="root_dir", type="folder", label="Library folder", default="Music"),
            ProviderSetting(key="cache_dir", type="folder", label="Cache", required=False),
            ProviderSetting(key="url", type="string", label="URL", default="http://x", required=False),
        ]

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def config(engine):
    return ConfigManager(engine=engine)


@pytest.fixture
def registry():
    return FakeRegistry(metadata=[FolderedProvider()])


@pytest.fixture
def service(session, config, registry):
    return SettingsService(session, config, registry)


@pytest.fixture
def admin(session):
    user = User(username="admin", password_hash="x", role="admin")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


KEY = "provider.foldery.root_dir"


# ------------------------------------------------------------------
# The declaration
# ------------------------------------------------------------------


def test_the_working_folder_is_a_folder_setting_with_no_default_and_the_catalog_has_no_other_folder():
    folder_settings = [s.key for s in CATALOG if s.type == "folder"]

    assert folder_settings == ["workspace_dir"]
    assert get_def("workspace_dir").default is None


def test_a_provider_folder_has_no_default_root_only_a_suggested_path():
    definition = provider_setting_def("foldery", FolderedProvider().config_schema()[0])

    assert definition.type == "folder" and definition.default is None and definition.suggested_path == "Music"
    assert definition.to_public()["suggested_path"] == "Music"


def test_a_folder_setting_without_a_suggestion_has_none():
    assert provider_setting_def("foldery", FolderedProvider().config_schema()[1]).suggested_path is None


def test_the_schema_of_the_application_lists_the_provider_folders_with_the_working_folder(registry):
    folder_keys = [s.key for s in merged_schema(registry) if s.type == "folder"]

    assert folder_keys == ["workspace_dir", KEY, "provider.foldery.cache_dir"]


# ------------------------------------------------------------------
# Coercion
# ------------------------------------------------------------------


def test_a_folder_value_is_coerced_to_its_stored_form():
    setting = get_def("workspace_dir")

    assert coerce_value(setting, {"root_id": "r1", "path": "a/b"}) == {"root_id": "r1", "path": "a/b"}
    assert coerce_value(setting, {"root_id": "r1", "path": ""}) == {"root_id": "r1", "path": ""}


@pytest.mark.parametrize("empty", ["", {}, None])
def test_an_empty_value_clears_the_folder(empty):
    assert coerce_value(get_def("workspace_dir"), empty) is None
    validate_value(get_def("workspace_dir"), None, FakeRegistry())  # optional: no value is fine


@pytest.mark.parametrize("raw, code", [
    ("/downloads/.work", rules.VALUE_INVALID), ({"root_id": "r1"}, rules.VALUE_INVALID),
    ({"root_id": "r1", "path": "../x"}, rules.PATH_INVALID), ({"root_id": "r1", "path": "/abs"}, rules.PATH_INVALID),
    ({"root_id": "r1", "path": "a//b"}, rules.PATH_INVALID), ([1], rules.VALUE_INVALID),
])
def test_what_is_not_a_root_and_a_clean_relative_path_is_refused_with_a_code(raw, code):
    with pytest.raises(SettingError) as refused:
        coerce_value(get_def("workspace_dir"), raw)

    assert refused.value.error_code == code


# ------------------------------------------------------------------
# Writing
# ------------------------------------------------------------------


def test_a_folder_of_an_existing_root_is_stored_and_read_back(service, session, engine, admin, tmp_path):
    root = add_root(engine, str(tmp_path))

    values = service.write_values({KEY: {"root_id": root.id, "path": "Music"}}, admin)

    assert values[KEY] == {"root_id": root.id, "path": "Music"}
    assert SettingsRepository(session).get_system(KEY) == {"root_id": root.id, "path": "Music"}


def test_an_unset_provider_folder_reads_none_not_its_suggestion(service, admin):
    assert service.effective_values(admin)[KEY] is None


def test_a_folder_can_be_cleared(service, admin, engine, tmp_path):
    root = add_root(engine, str(tmp_path))
    service.write_values({KEY: {"root_id": root.id, "path": ""}}, admin)

    values = service.write_values({"provider.foldery.cache_dir": ""}, admin)
    assert values["provider.foldery.cache_dir"] is None


def test_a_folder_of_a_root_that_does_not_exist_is_refused(service, admin):
    with pytest.raises(SettingsWriteError) as refused:
        service.write_values({KEY: {"root_id": "ghost", "path": "x"}}, admin)

    assert refused.value.status_code == 400 and refused.value.detail["error_code"] == rules.ROOT_NOT_FOUND


def test_a_folder_of_a_root_the_gateway_refuses_is_refused_with_the_reason(service, admin, engine):
    root = add_root(engine, "/etc", state="refused", reason="not a mounted volume")

    with pytest.raises(SettingsWriteError) as refused:
        service.write_values({KEY: {"root_id": root.id, "path": "x"}}, admin)

    assert refused.value.detail["error_code"] == rules.ROOT_REFUSED and "not a mounted volume" in refused.value.detail["error_message"]


def test_a_path_that_leaves_the_root_is_refused_whatever_spells_it(service, admin, engine, tmp_path):
    root = add_root(engine, str(tmp_path))

    for path in ("..", "a/../..", "/etc/passwd"):
        with pytest.raises(SettingsWriteError) as refused:
            service.write_values({KEY: {"root_id": root.id, "path": path}}, admin)
        assert refused.value.detail["error_code"] == rules.PATH_INVALID


def test_a_link_inside_the_root_that_leads_out_of_it_is_refused(service, admin, engine, tmp_path):
    root_dir, outside = tmp_path / "root", tmp_path / "outside"
    root_dir.mkdir()
    outside.mkdir()
    (root_dir / "escape").symlink_to(outside)
    root = add_root(engine, str(root_dir))

    with pytest.raises(SettingsWriteError) as refused:
        service.write_values({KEY: {"root_id": root.id, "path": "escape/deeper"}}, admin)

    assert refused.value.detail["error_code"] == rules.LEAVES_ROOT


def test_the_working_folder_needs_a_root_the_application_sees_mounted(service, admin, engine, tmp_path):
    """The host writes into the working folder, so its root has to exist and be writable where the application runs."""
    missing = add_root(engine, str(tmp_path / "not-mounted-here"))
    mounted = add_root(engine, str(tmp_path))

    with pytest.raises(SettingsWriteError) as refused:
        service.write_values({"workspace_dir": {"root_id": missing.id, "path": ".musizeer-work"}}, admin)
    assert refused.value.detail["error_code"] == rules.NOT_MOUNTED

    service.write_values({"workspace_dir": {"root_id": mounted.id, "path": ".musizeer-work"}}, admin)
    # a provider's folder is written by the provider in the gateway: the application need not see it
    service.write_values({KEY: {"root_id": missing.id, "path": "x"}}, admin)


def test_a_batch_with_a_bad_folder_writes_nothing(service, admin, engine, tmp_path):
    root = add_root(engine, str(tmp_path))

    with pytest.raises(SettingsWriteError):
        service.write_values({KEY: {"root_id": root.id, "path": "ok"}, "provider.foldery.cache_dir": {"root_id": "ghost", "path": ""}}, admin)

    assert service.effective_values(admin)[KEY] is None


def test_the_folders_among_some_keys_are_found_by_type_not_by_name(service):
    assert service.folder_keys_among(["workspace_dir", KEY, "auth_enabled", "provider.foldery.url", "unknown"]) == ["workspace_dir", KEY]


# ------------------------------------------------------------------
# What a provider receives
# ------------------------------------------------------------------


def stored(engine, **values):
    with Session(engine) as session:
        for key, value in values.items():
            SettingsRepository(session).set_system(f"provider.foldery.{key}", value)


@pytest.fixture
def context(engine, monkeypatch):
    monkeypatch.setattr("db.engine.engine", engine)  # the host's context reads the shared engine
    context = HostProviderContext("foldery")
    context.declare_settings(FolderedProvider().config_schema())
    return context


def test_the_snapshot_carries_the_absolute_path_a_folder_names_and_never_a_root(context, engine):
    library = add_root(engine, "/library")
    stored(engine, root_dir={"root_id": library.id, "path": "Music/Rock"}, url="http://upstream")

    settings = context.snapshot().settings

    assert settings == {"root_dir": "/library/Music/Rock", "url": "http://upstream"}
    assert library.id not in str(settings)


def test_the_folder_setting_is_the_whole_root_when_its_path_is_empty(context, engine):
    library = add_root(engine, "/library")
    stored(engine, root_dir={"root_id": library.id, "path": ""})

    assert context.snapshot().settings["root_dir"] == "/library"


def test_a_folder_that_is_not_set_is_left_out_so_the_provider_answers_not_configured(context, engine):
    stored(engine, url="http://upstream")

    assert context.snapshot().settings == {"url": "http://upstream"}


@pytest.mark.parametrize("root_options", [{"state": "refused", "reason": "not writable"}])
def test_a_folder_in_a_root_the_gateway_refuses_is_left_out_too(context, engine, root_options):
    library = add_root(engine, "/library", **root_options)
    stored(engine, root_dir={"root_id": library.id, "path": "Music"})

    assert "root_dir" not in context.snapshot().settings


def test_an_old_text_or_a_deleted_root_is_left_out(context, engine):
    stored(engine, root_dir="/library/Music")
    assert "root_dir" not in context.snapshot().settings

    stored(engine, root_dir={"root_id": "deleted", "path": ""})
    assert "root_dir" not in context.snapshot().settings


def test_the_gateway_is_granted_the_resolved_path_of_the_setting_it_names(context, engine):
    library = add_root(engine, "/library")
    stored(engine, root_dir={"root_id": library.id, "path": "Music"})

    assert path_values(FolderedProvider.permissions, context.snapshot().settings) == {"root_dir": "/library/Music"}


def test_a_nested_root_resolves_to_the_same_folder_whichever_root_holds_it(context, engine):
    outer, inner = add_root(engine, "/media"), add_root(engine, "/media/Musica")
    stored(engine, root_dir={"root_id": outer.id, "path": "Musica/Rock"}, cache_dir={"root_id": inner.id, "path": "Rock"})

    settings = context.snapshot().settings

    assert settings["root_dir"] == settings["cache_dir"] == "/media/Musica/Rock"


def test_the_other_settings_of_the_snapshot_do_not_touch_the_roots_table(context, engine, monkeypatch):
    stored(engine, url="http://upstream")
    monkeypatch.setattr(folders, "roots_by_id", lambda *a, **k: (_ for _ in ()).throw(AssertionError("roots read for no folder")))

    assert context.snapshot().settings == {"url": "http://upstream"}
