"""``musizeer_sdk.conformance``: the generic rules a provider's own test suite
runs on its instance, with good and bad sample providers."""
from __future__ import annotations

import pytest

from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow, OAuth2Config
from musizeer_sdk.capabilities import (
    CAPABILITY_PROPAGATE_FAVORITES,
    CAPABILITY_UNLIKE_SAVED_TRACK,
    CAPABILITY_USER_LIBRARY,
)
from musizeer_sdk.conformance import (
    CAPABILITIES_NEEDING_A_SCOPE,
    ConformanceError,
    check_config_schema,
    check_folders,
    check_oauth2_capability_scopes,
    check_provider,
    config_schema_violations,
    declared_capabilities,
    folder_violations,
    oauth2_capability_violations,
    provider_violations,
)
from musizeer_sdk.context import StaticProviderContext
from musizeer_sdk.interfaces import MetadataProvider, StorageProvider, SyncCandidate, SyncProvider
from musizeer_sdk.permissions import FilesystemAccess, Permissions
from musizeer_sdk.settings import ProviderSetting

from sample_providers import Base, SampleMetadata

OAUTH2 = AuthDefinition(
    flow=AuthFlow.OAUTH2,
    account_model=AccountModel.PERSONAL,
    oauth2=OAuth2Config(authorize_url="https://auth.example/authorize", token_url="https://auth.example/token",
                        scopes=["read"], capability_scopes={CAPABILITY_UNLIKE_SAVED_TRACK: ["library-modify"]}),
)


def make_provider(*, schema=None, auth=None, supports=(), name="sample"):
    """A metadata provider that declares ``supports`` (names of ``supports_*``
    queries it answers True to) and the given schema and auth."""

    class Sample(Base, MetadataProvider):
        pass

    Sample.name = name
    if auth is not None:
        Sample.auth = auth
    if schema is not None:
        Sample.config_schema = lambda self: schema  # type: ignore[method-assign]
    for query in supports:
        setattr(Sample, query, lambda self: True)
    return Sample(StaticProviderContext())


def setting(key="k", type="string", **kwargs):
    return ProviderSetting(key=key, type=type, label=kwargs.pop("label", key), **kwargs)


# --- good providers ---------------------------------------------------------------


def test_a_provider_that_follows_the_rules_passes_every_check():
    provider = SampleMetadata(StaticProviderContext())

    check_provider(provider)
    check_config_schema(provider)
    check_oauth2_capability_scopes(provider)

    assert provider_violations(provider) == []


def test_a_well_formed_schema_with_every_kind_of_setting_has_no_violation():
    schema = [
        setting("flag", "bool", default=True),
        setting("count", "int", default=3, minimum=1, maximum=9, step=1),
        setting("ratio", "float", default=1, minimum=0.5, maximum=2.0),
        setting("name", "string", default="x"),
        setting("mode", "enum", default="a", options=("a", "b")),
        setting("tags", "list", default=["x"]),
        setting("url", "string", visible_when=(("mode", "b"), ("flag", True))),
        setting("empty", "string", default=None, required=False),
    ]

    assert config_schema_violations(make_provider(schema=schema)) == []


def test_an_oauth2_provider_that_maps_its_scopes_passes():
    provider = make_provider(auth=OAUTH2, supports=("supports_user_library", "supports_unlike_saved_track"))

    check_provider(provider)
    assert declared_capabilities(provider) == [CAPABILITY_USER_LIBRARY, CAPABILITY_UNLIKE_SAVED_TRACK]


def test_a_provider_that_is_not_oauth2_has_nothing_to_map():
    provider = make_provider(supports=("supports_unlike_saved_track",))

    assert oauth2_capability_violations(provider) == []


# --- config_schema() ----------------------------------------------------------------


@pytest.mark.parametrize("schema, expected", [
    ("not a list", "must return a list"),
    ([{"key": "k"}], "must be ProviderSetting, found dict"),
    ([setting("")], "empty or non-string key"),
    ([setting("k"), setting("k")], "setting 'k' is declared more than once"),
    ([setting("k", "number")], "has the type 'number'"),
    ([setting("k", "enum")], "is an enum and declares no options"),
    ([setting("k", "enum", options=("a", "a"))], "lists an option more than once"),
    ([setting("k", "enum", options=("a", "b"), default="c")], "default 'c', which is not one of its options"),
    ([setting("k", "int", default="3")], "is a int and its default is '3'"),
    ([setting("k", "int", default=True)], "is a int and its default is True"),
    ([setting("k", "bool", default=1)], "is a bool and its default is 1"),
    ([setting("k", "int", minimum=5, maximum=1)], "minimum (5) above its maximum (1)"),
    ([setting("k", "int", step=0)], "step that is not positive"),
    ([setting("k", label="")], "has no label"),
    ([setting("k", visible_when=(("missing", True),))], "declares no such setting"),
    ([setting("k", visible_when=(("k", True),))], "names its own key"),
    ([setting("k", visible_when=("mode",))], "not a (key, value) pair"),
    ([setting("mode", "enum", options=("a", "b")), setting("k", visible_when=(("mode", "z"),))],
     "which is not one of its options"),
])
def test_a_malformed_schema_is_reported(schema, expected):
    violations = config_schema_violations(make_provider(schema=schema))

    assert any(expected in violation for violation in violations), violations
    with pytest.raises(ConformanceError, match="breaks"):
        check_config_schema(make_provider(schema=schema))


def test_a_schema_that_cannot_be_read_is_a_violation():
    provider = make_provider()

    def boom():
        raise RuntimeError("no settings today")

    provider.config_schema = boom  # type: ignore[method-assign]

    assert config_schema_violations(provider) == ["config_schema() raised RuntimeError: no settings today"]


# --- OAuth2 capability scopes ------------------------------------------------------------


def test_a_capability_that_needs_a_scope_and_is_not_mapped_is_reported():
    auth = AuthDefinition(
        flow=AuthFlow.OAUTH2, account_model=AccountModel.PERSONAL,
        oauth2=OAuth2Config(authorize_url="https://a.example/a", token_url="https://a.example/t", scopes=["read"]),
    )
    provider = make_provider(auth=auth, supports=("supports_unlike_saved_track",), name="writer")

    violations = oauth2_capability_violations(provider)

    assert len(violations) == 1 and "'unlike_saved_track'" in violations[0] and "maps no scope" in violations[0]
    with pytest.raises(ConformanceError) as raised:
        check_oauth2_capability_scopes(provider)
    assert raised.value.provider == "writer" and raised.value.violations == tuple(violations)


def test_a_mapping_to_no_scope_does_not_count():
    auth = AuthDefinition(
        flow=AuthFlow.OAUTH2, account_model=AccountModel.PERSONAL,
        oauth2=OAuth2Config(authorize_url="https://a.example/a", token_url="https://a.example/t", scopes=["read"],
                            capability_scopes={CAPABILITY_UNLIKE_SAVED_TRACK: []}),
    )
    provider = make_provider(auth=auth, supports=("supports_unlike_saved_track",))

    violations = oauth2_capability_violations(provider)

    assert any("maps no scope" in v for v in violations) and any("non-empty list of scope names" in v for v in violations)


def test_a_mapping_for_a_capability_the_provider_does_not_declare_is_reported():
    # OAUTH2 declares the unlike mapping, the provider does not support it.
    provider = make_provider(auth=OAUTH2)

    violations = oauth2_capability_violations(provider)

    assert len(violations) == 1 and "does not declare" in violations[0]


def test_a_mapping_for_an_unknown_capability_is_reported():
    auth = AuthDefinition(
        flow=AuthFlow.OAUTH2, account_model=AccountModel.PERSONAL,
        oauth2=OAuth2Config(authorize_url="https://a.example/a", token_url="https://a.example/t", scopes=["read"],
                            capability_scopes={"unlike_saved_trak": ["library-modify"]}),
    )

    violations = oauth2_capability_violations(make_provider(auth=auth))

    assert violations == ["OAuth2Config.capability_scopes maps 'unlike_saved_trak', which is not a capability of the contract"]


def test_an_oauth2_flow_without_its_configuration_is_reported():

    auth = AuthDefinition(flow=AuthFlow.OAUTH2, account_model=AccountModel.PERSONAL, oauth2=None)

    assert oauth2_capability_violations(make_provider(auth=auth)) == [
        "the auth flow is OAUTH2 and the provider gives no oauth2 configuration"
    ]


def test_a_sync_provider_that_propagates_favorites_over_oauth2_has_to_map_it():
    class Sync(Base, SyncProvider):
        name = "syncer"
        auth = AuthDefinition(
            flow=AuthFlow.OAUTH2, account_model=AccountModel.PERSONAL,
            oauth2=OAuth2Config(authorize_url="https://a.example/a", token_url="https://a.example/t", scopes=["read"]),
        )

        def trigger_scan(self, session):
            return None

        def find_candidates(self, track, session) -> list[SyncCandidate]:
            return []

        def get_stream_url(self, sync_id, session):
            return None

        def supports_likes(self) -> bool:
            return True

    violations = oauth2_capability_violations(Sync(StaticProviderContext()))

    assert CAPABILITY_PROPAGATE_FAVORITES in CAPABILITIES_NEEDING_A_SCOPE
    assert len(violations) == 1 and "'propagate_favorites'" in violations[0]


# --- check_provider lists everything -------------------------------------------------------


def test_check_provider_lists_every_violation_of_every_rule():
    auth = AuthDefinition(
        flow=AuthFlow.OAUTH2, account_model=AccountModel.PERSONAL,
        oauth2=OAuth2Config(authorize_url="https://a.example/a", token_url="https://a.example/t", scopes=["read"]),
    )
    provider = make_provider(
        schema=[setting("k"), setting("k"), setting("m", "enum")], auth=auth,
        supports=("supports_unlike_saved_track",), name="messy",
    )

    with pytest.raises(ConformanceError) as raised:
        check_provider(provider)

    error = raised.value
    assert isinstance(error, AssertionError)
    assert len(error.violations) == 3
    text = str(error)
    assert text.startswith("provider 'messy' breaks 3 rule(s) of the contract:")
    assert all(f"  - {violation}" in text for violation in error.violations)


# --- folders (ADR-0008) ---------------------------------------------------------------------


def make_storage(*, schema=None, permissions=None, library=None, name="samplestore"):
    """A storage provider with the given settings, granted folders and declared library folder."""

    class Store(Base, StorageProvider):
        def store(self, source, relative_dest, session):
            return relative_dest

        def exists(self, relative_path, session):
            return True

        def stream(self, relative_path, workspace, session):
            raise NotImplementedError

        def delete(self, relative_path, session):
            return True

    Store.name = name
    if schema is not None:
        Store.config_schema = lambda self: schema  # type: ignore[method-assign]
    if permissions is not None:
        Store.permissions = permissions  # type: ignore[attr-defined]
    if library is not None:
        Store.library_folder_setting = lambda self: library  # type: ignore[method-assign]
    return Store(StaticProviderContext())


GOOD_STORAGE_SETTINGS = [
    setting("root_dir", "folder", label="Library folder", default="Music"),
    setting("elsewhere", "folder", label="Anywhere", default=None, required=False),
    setting("base", "folder", label="Base", default=""),
]


def test_a_storage_provider_that_declares_a_folder_setting_as_its_library_passes():
    provider = make_storage(
        schema=GOOD_STORAGE_SETTINGS, library="root_dir",
        permissions=Permissions(filesystem_from_settings=(FilesystemAccess("root_dir", "rw"),)),
    )

    check_provider(provider)
    check_folders(provider)

    assert folder_violations(provider) == [] and provider.library_folder_setting() == "root_dir"


def test_the_library_folder_setting_of_a_storage_provider_defaults_to_none():
    assert make_storage().library_folder_setting() is None
    assert folder_violations(make_storage()) == []


@pytest.mark.parametrize("default", ["", "a", "a/b", "Music/Rock", "name with spaces/and.dots", "a\\b"])
def test_a_relative_normalized_default_is_a_good_suggestion(default):
    assert folder_violations(make_provider(schema=[setting("f", "folder", default=default)])) == []


@pytest.mark.parametrize("default, expected", [
    ("/music", "is absolute"),
    ("../x", "'..' segment"),
    ("a/../b", "'..' segment"),
    ("a//b", "empty segment"),
    ("a/", "empty segment"),
    ("./a", "'.' segment"),
    ("a\0b", "NUL"),
    ("x" * 5000, "longer than 4096"),
    (5, "is not text"),
])
def test_a_folder_default_that_is_not_a_relative_normalized_path_is_reported(default, expected):
    provider = make_provider(schema=[setting("f", "folder", default=default)])

    violations = folder_violations(provider)

    assert len(violations) == 1 and expected in violations[0] and "setting 'f'" in violations[0], violations
    with pytest.raises(ConformanceError, match="suggested path inside the root folder"):
        check_folders(provider)
    # the schema rule alone is not the one that catches an absolute default of a folder
    assert provider_violations(provider) == config_schema_violations(provider) + violations


def test_a_secret_folder_is_reported():
    violations = folder_violations(make_provider(schema=[setting("f", "folder", secret=True)]))

    assert len(violations) == 1 and "declared secret" in violations[0]


def test_a_setting_granted_to_the_sandbox_that_is_not_a_folder_is_reported():
    provider = make_storage(
        schema=[setting("root_dir", "string", default="/library"), setting("ok", "folder")],
        permissions=Permissions(filesystem_from_settings=(FilesystemAccess("root_dir", "rw"), FilesystemAccess("ok", "ro"))),
    )

    violations = folder_violations(provider)

    assert len(violations) == 1
    assert "filesystem_from_settings names 'root_dir', which is a string setting" in violations[0]


def test_a_granted_setting_that_is_not_declared_is_reported():
    provider = make_storage(schema=[], permissions=Permissions(filesystem_from_settings=(FilesystemAccess("ghost", "rw"),)))

    violations = folder_violations(provider)

    assert len(violations) == 1 and "'ghost'" in violations[0] and "no such setting" in violations[0]


@pytest.mark.parametrize("schema, expected", [
    ([setting("root_dir", "string")], "which is a string setting, not a folder"),
    ([], "declares no setting for"),
])
def test_a_library_folder_setting_that_is_not_a_declared_folder_is_reported(schema, expected):
    violations = folder_violations(make_storage(schema=schema, library="root_dir"))

    assert len(violations) == 1 and "library_folder_setting() names 'root_dir'" in violations[0] and expected in violations[0]


def test_every_violation_of_a_messy_storage_provider_is_listed_at_once():
    provider = make_storage(
        schema=[setting("root_dir", "folder", default="/abs", secret=True), setting("remote", "string")],
        permissions=Permissions(filesystem_from_settings=(FilesystemAccess("remote", "rw"),)),
        library="remote", name="messy-storage",
    )

    with pytest.raises(ConformanceError) as raised:
        check_provider(provider)

    assert len(raised.value.violations) == 4, raised.value.violations
    assert str(raised.value).startswith("provider 'messy-storage' breaks 4 rule(s) of the contract:")


def test_a_non_storage_provider_has_no_library_folder_to_check():
    assert folder_violations(make_provider(schema=[setting("f", "folder")])) == []
