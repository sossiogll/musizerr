"""The manifest declares what the host used to infer from the provider's class."""
from __future__ import annotations

import inspect

import pytest

from musizeer_sdk import manifest, wire
from musizeer_sdk.auth import AuthFlow
from musizeer_sdk.context import StaticProviderContext
from musizeer_sdk.interfaces import BaseProvider, LyricsProvider, PROVIDER_API_VERSION
from musizeer_sdk.manifest import ManifestError, build
from sample_providers import (
    NO_AUTH,
    SampleDownload,
    SampleLyrics,
    SampleMetadata,
    SampleStorage,
    SampleSync,
)

CONTEXT = StaticProviderContext()


def test_a_manifest_names_the_provider_its_roles_its_auth_and_its_settings():
    built = build(SampleMetadata(CONTEXT))

    assert (built.name, built.label, built.roles) == ("samplemeta", "Sample", ("metadata",))
    assert built.auth.flow == AuthFlow.NONE
    assert [setting.key for setting in built.config_schema] == ["market"]
    assert built.provider_api_version is None


def test_a_provider_that_fills_two_roles_says_so():
    assert build(SampleSync(CONTEXT)).roles == ("metadata", "sync")


def test_a_package_declares_the_version_of_the_contract_it_implements():
    class Packaged(SampleLyrics):
        provider_api_version = PROVIDER_API_VERSION

    assert build(Packaged(CONTEXT)).provider_api_version == PROVIDER_API_VERSION


def test_every_capability_the_provider_declares_is_in_the_manifest():
    meta = build(SampleMetadata(CONTEXT)).declarations
    assert meta["streams_full_tracks"] is True
    assert meta["supports_discography"] is True
    assert meta["supports_isrc_lookup"] is True
    assert meta["supports_user_library"] is False
    assert meta["restricts_playlist_tracks"] is False
    assert meta["supports_login"] is False
    assert meta["is_library_available"] is None

    download = build(SampleDownload(CONTEXT)).declarations
    assert download["supported_identifier_types"] == frozenset({"isrc"})
    assert (download["max_concurrent_downloads"], download["supports_fuzzy"], download["enabled_by_default"]) == (2, True, True)

    assert build(SampleStorage(CONTEXT)).declarations["supports_stream_offset"] is True
    sync = build(SampleSync(CONTEXT)).declarations
    assert (sync["supports_likes"], sync["supports_playlists"]) == (True, False)
    assert "streams_full_tracks" in sync, "a sync provider that is also a metadata provider declares both"


def test_a_declaration_set_on_the_instance_is_read_too():
    provider = SampleDownload(CONTEXT)
    provider.supports_fuzzy = False
    provider.supported_identifier_types = {"mbid", "isrc"}

    declarations = build(provider).declarations

    assert declarations["supports_fuzzy"] is False
    assert declarations["supported_identifier_types"] == frozenset({"mbid", "isrc"})


def test_the_optional_methods_a_provider_implements_are_listed():
    implements = set(build(SampleMetadata(CONTEXT)).implements)

    assert {"search_tracks", "find_by_isrc", "iter_saved_tracks", "unlike_saved_track", "get_new_releases"} <= implements
    assert {"search_albums", "find_related_tracks", "get_saved_albums"}.isdisjoint(implements)
    assert {"validate_config", "test_connection"} <= implements, "the abstract ones are implemented by definition"


def test_a_method_overridden_on_the_instance_counts_as_implemented():
    provider = SampleLyrics(CONTEXT)
    provider.get_cover_url = lambda cover_id, session: "x"

    assert "get_cover_url" in build(provider).implements
    assert "get_cover_url" not in build(SampleLyrics(CONTEXT)).implements


def test_the_manifest_survives_the_wire():
    built = build(SampleSync(CONTEXT))

    assert wire.loads(wire.dumps(built)) == built


@pytest.mark.parametrize("missing", ["name", "label", "auth"])
def test_a_provider_without_a_name_a_label_or_an_auth_declaration_cannot_be_described(missing):
    namespace = {
        "name": "x", "label": "X", "auth": NO_AUTH, "get_lyrics": lambda self, track, session: None,
        "validate_config": lambda self, config: True, "test_connection": lambda self, session: None,
    }
    del namespace[missing]
    broken = type("Broken", (LyricsProvider,), namespace)

    with pytest.raises(ManifestError, match=missing):
        build(broken(CONTEXT))


def test_an_auth_that_is_not_an_auth_definition_is_refused():
    broken = type("Broken", (SampleLyrics,), {"auth": "none"})

    with pytest.raises(ManifestError, match="AuthDefinition"):
        build(broken(CONTEXT))


def test_the_contract_methods_are_the_public_functions_of_the_roles_it_fills():
    methods = manifest.contract_methods(SampleLyrics(CONTEXT))

    assert {"get_lyrics", "validate_config", "test_connection", "get_cover_url", "supports_login"} <= methods
    assert "search_tracks" not in methods and "_private" not in methods
    assert "search_tracks" in manifest.contract_methods(SampleSync(CONTEXT))


# --- the declarations cover the contract ------------------------------------

ABCS = [BaseProvider, *manifest.ROLES.values()]


def _no_argument_queries(abc):
    for name, member in vars(abc).items():
        if name.startswith("_") or not inspect.isfunction(member):
            continue
        parameters = [p for p in inspect.signature(member).parameters.values() if p.name != "self"]
        if not parameters and name.startswith(("supports_", "restricts_", "is_")):
            yield name


@pytest.mark.parametrize("abc", ABCS, ids=lambda a: a.__name__)
def test_every_capability_query_of_the_contract_is_declared_in_the_manifest(abc):
    declared = set(manifest.DECLARED_QUERIES.get(abc, ()))
    # IdentificationProvider.is_available answers from the provider's settings
    # (an API key set), so it stays a call, with a context snapshot.
    call_only = {"is_available"}

    assert set(_no_argument_queries(abc)) - call_only == declared


@pytest.mark.parametrize("abc", ABCS, ids=lambda a: a.__name__)
def test_every_declared_name_exists_on_the_contract(abc):
    for name in manifest.DECLARED_QUERIES.get(abc, ()):
        assert inspect.isfunction(vars(abc)[name]), name
    for name in manifest.DECLARED_ATTRIBUTES.get(abc, ()):
        assert name in vars(abc), name


def test_every_class_level_declaration_of_the_contract_is_declared_in_the_manifest():
    # Attributes the contract defines with a default value on a role class
    # (``supports_fuzzy = False``, ``max_concurrent_downloads = None``...).
    for abc in manifest.ROLES.values():
        plain = {
            name for name, member in vars(abc).items()
            if not name.startswith("_") and not callable(member) and name not in {"name", "label", "auth"}
            and not isinstance(member, (property,))
        }
        assert plain <= set(manifest.DECLARED_ATTRIBUTES.get(abc, ())), (abc.__name__, plain)


def test_the_declarations_of_a_provider_with_every_role_are_all_present():
    class Everything(SampleMetadata, SampleDownload, SampleLyrics, SampleStorage, SampleSync):
        name = "everything"

    declarations = build(Everything(CONTEXT)).declarations
    expected = set()
    for abc in ABCS:
        expected |= set(manifest.DECLARED_ATTRIBUTES.get(abc, ())) | set(manifest.DECLARED_QUERIES.get(abc, ()))

    assert set(declarations) == expected
