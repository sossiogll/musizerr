"""``musizerr_sdk.conformance``: the generic rules a provider's own test suite
runs on its instance, with good and bad sample providers."""
from __future__ import annotations

import pytest

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow, OAuth2Config
from musizerr_sdk.capabilities import (
    CAPABILITY_PROPAGATE_FAVORITES,
    CAPABILITY_UNLIKE_SAVED_TRACK,
    CAPABILITY_USER_LIBRARY,
)
from musizerr_sdk.conformance import (
    CAPABILITIES_NEEDING_A_SCOPE,
    ConformanceError,
    check_config_schema,
    check_oauth2_capability_scopes,
    check_provider,
    config_schema_violations,
    declared_capabilities,
    oauth2_capability_violations,
    provider_violations,
)
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.interfaces import MetadataProvider, SyncCandidate, SyncProvider
from musizerr_sdk.settings import ProviderSetting

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
