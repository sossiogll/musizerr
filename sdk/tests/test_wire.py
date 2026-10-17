"""
The wire codec is lossless for every type that crosses the contract: for each
registered type ``decode(encode(x)) == x``, and the encoded form survives
``json.dumps`` / ``json.loads``. A type added to the SDK without a strategy
here fails ``test_every_wire_type_has_a_strategy``.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from musizerr_sdk import auth, delegation, errors, interfaces, manifest, models, wire
from musizerr_sdk.context import ContextSnapshot
from musizerr_sdk.identifiers import Identifier
from musizerr_sdk.permissions import FilesystemAccess, Permissions, ResourceLimits, TcpAccess
from musizerr_sdk.settings import ProviderSetting

SETTINGS = settings(
    max_examples=60, deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much],
)

text = st.text(max_size=20)
short = st.text(max_size=8)
maybe_text = st.none() | text
maybe_int = st.none() | st.integers(min_value=-(2**40), max_value=2**40)
small_int = st.integers(min_value=0, max_value=10**6)


def json_values(max_leaves: int = 10):
    return st.recursive(
        st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | text,
        lambda children: st.lists(children, max_size=4) | st.dictionaries(text, children, max_size=4),
        max_leaves=max_leaves,
    )


json_dicts = st.dictionaries(text, json_values(), max_size=4)

fixed_offsets = st.integers(min_value=-86399, max_value=86399).map(lambda s: timezone(timedelta(seconds=s)))
datetimes = st.datetimes(
    min_value=datetime(1, 1, 3), max_value=datetime(9999, 12, 29), timezones=st.none() | fixed_offsets,
)

identifiers = st.builds(Identifier, type=short, value=text, provider=maybe_text)
album_refs = st.builds(models.AlbumRef, name=text, provider=short, provider_id=short)
artist_refs = st.builds(models.ArtistRef, name=text, provider=short, provider_id=short)
tracks = st.builds(
    models.Track,
    title=text, artist=text, album=text, duration_ms=small_int,
    cover_url=maybe_text, preview_url=maybe_text, stream_url=maybe_text,
    provider=short, provider_id=short, artist_provider_id=short, album_provider_id=short,
    internal_id=short,
    external_ids=st.lists(identifiers, max_size=3),
    year=maybe_int,
    albums=st.lists(album_refs, max_size=2), artists=st.lists(artist_refs, max_size=2),
    providers=st.lists(short, max_size=3),
    saved_at=st.none() | datetimes,
    track_number=maybe_int, disc_number=maybe_int,
)
unavailable = st.builds(models.TracksUnavailable, code=short, message=text)
credential_fields = st.builds(auth.CredentialField, key=short, label=text, secret=st.booleans())

STRATEGIES: dict[type, st.SearchStrategy] = {
    Identifier: identifiers,
    models.AlbumRef: album_refs,
    models.ArtistRef: artist_refs,
    models.Track: tracks,
    models.Album: st.builds(
        models.Album, title=text, artist=text, year=maybe_int, track_count=maybe_int, cover_url=maybe_text,
        provider=short, provider_id=short, artist_provider_id=short, internal_id=short,
        external_ids=st.lists(identifiers, max_size=3), artists=st.lists(artist_refs, max_size=2),
        tracks=st.lists(tracks, max_size=3), providers=st.lists(short, max_size=3), album_type=maybe_text,
    ),
    models.Artist: st.builds(
        models.Artist, name=text, provider=short, provider_id=short, cover_url=maybe_text, internal_id=short,
        external_ids=st.lists(identifiers, max_size=3), providers=st.lists(short, max_size=3),
    ),
    models.Playlist: st.builds(
        models.Playlist, name=text, provider=short, provider_id=short, internal_id=short, description=text,
        cover_url=maybe_text, tracks=st.lists(tracks, max_size=3), track_count=maybe_int,
        tracks_unavailable=st.none() | unavailable, snapshot_id=maybe_text,
    ),
    models.LrcLine: st.builds(models.LrcLine, time_ms=small_int, text=text),
    models.Lyrics: st.builds(
        models.Lyrics, plain=maybe_text, synced=st.lists(st.builds(models.LrcLine, time_ms=small_int, text=text), max_size=3),
        source=short,
    ),
    models.LibraryItem: st.builds(
        models.LibraryItem, sync_id=short, path=text, title=text, artist=text, album=text, year=maybe_int,
        duration_ms=small_int, size=small_int, suffix=short, bitrate=small_int, sample_rate=small_int,
    ),
    models.TracksUnavailable: unavailable,
    auth.CredentialField: credential_fields,
    auth.AccountIdentity: st.builds(auth.AccountIdentity, external_id=short, display_name=maybe_text),
    auth.BasicClientAuth: st.builds(auth.BasicClientAuth),
    auth.RequestBodyClientAuth: st.builds(auth.RequestBodyClientAuth),
    auth.BearerHeaderRequestAuth: st.builds(auth.BearerHeaderRequestAuth),
    auth.QueryParamRequestAuth: st.builds(
        auth.QueryParamRequestAuth, client_id_param=maybe_text, access_token_param=maybe_text,
        require_client_id=st.booleans(), require_access_token=st.booleans(),
    ),
    auth.ClientCredentialsFallback: st.builds(auth.ClientCredentialsFallback),
    auth.PublicClientFallback: st.builds(auth.PublicClientFallback),
    auth.OAuth2Config: st.builds(
        auth.OAuth2Config, authorize_url=text, token_url=text, scopes=st.lists(short, max_size=3),
        redirect_uri=text,
        token_client_auth=st.none() | st.builds(auth.BasicClientAuth) | st.builds(auth.RequestBodyClientAuth),
        request_auth=st.none() | st.builds(auth.BearerHeaderRequestAuth)
        | st.builds(auth.QueryParamRequestAuth, client_id_param=maybe_text, access_token_param=maybe_text),
        fallback_auth=st.none() | st.builds(auth.ClientCredentialsFallback) | st.builds(auth.PublicClientFallback),
        capability_scopes=st.dictionaries(short, st.lists(short, max_size=2), max_size=2),
        extra_authorize_params=st.dictionaries(short, text, max_size=2),
    ),
    auth.ApiKeyConfig: st.builds(auth.ApiKeyConfig, header_name=maybe_text, query_param=maybe_text),
    auth.AccountDisplayField: st.builds(auth.AccountDisplayField, key=short, label=text),
    auth.ExternalUtilityConfig: st.builds(
        auth.ExternalUtilityConfig, credential_type=short, borrow_oauth_client_from=maybe_text,
        library_package=maybe_text, library_url=maybe_text, disclaimer=maybe_text, setup_command=maybe_text,
        setup_instructions=maybe_text,
        intake_name=st.one_of(st.none(), st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True)),
        account_display_fields=st.lists(st.builds(auth.AccountDisplayField, key=short, label=text), max_size=2),
    ),
    auth.AuthDefinition: st.one_of(
        st.builds(auth.AuthDefinition, flow=st.just(auth.AuthFlow.NONE), account_model=st.just(auth.AccountModel.NONE)),
        st.builds(
            auth.AuthDefinition, flow=st.just(auth.AuthFlow.API_KEY), account_model=st.just(auth.AccountModel.SYSTEM),
            api_key=st.builds(auth.ApiKeyConfig, header_name=maybe_text, query_param=maybe_text),
        ),
        st.builds(
            auth.AuthDefinition, flow=st.just(auth.AuthFlow.OAUTH2),
            account_model=st.sampled_from([m for m in auth.AccountModel if m is not auth.AccountModel.NONE]),
            oauth2=st.builds(auth.OAuth2Config, authorize_url=text, token_url=text, scopes=st.lists(short, max_size=2)),
        ),
        st.builds(
            auth.AuthDefinition, flow=st.just(auth.AuthFlow.CREDENTIALS),
            account_model=st.sampled_from([auth.AccountModel.PERSONAL, auth.AccountModel.SYSTEM]),
            credential_fields=st.lists(credential_fields, max_size=3).map(tuple),
        ),
        st.builds(
            auth.AuthDefinition, flow=st.just(auth.AuthFlow.EXTERNAL_UTILITY),
            account_model=st.just(auth.AccountModel.SYSTEM),
            external_utility=st.builds(auth.ExternalUtilityConfig, credential_type=short),
        ),
    ),
    auth.AuthSession: st.builds(
        auth.AuthSession, flow=st.sampled_from(list(auth.AuthFlow)), headers=json_dicts, params=json_dicts,
        extra=json_dicts,
    ),
    delegation.FileRef: st.builds(
        delegation.FileRef, path=text, quality_lossless=st.booleans(), bitrate=small_int, sample_rate=small_int,
        external_id=maybe_text,
    ),
    delegation.ImportConflict: st.builds(
        delegation.ImportConflict, track_key=short,
        incoming=st.none() | st.builds(delegation.FileRef, path=text),
        existing=st.none() | st.builds(delegation.FileRef, path=text),
    ),
    delegation.DelegationStatus: st.builds(
        delegation.DelegationStatus, state=st.sampled_from([
            delegation.DOWNLOADING, delegation.IMPORTING, delegation.CONFLICT, delegation.DISPATCHED,
            delegation.DOWNLOADED, delegation.FAILED,
        ]),
        conflicts=st.lists(st.builds(delegation.ImportConflict, track_key=short), max_size=2), detail=maybe_text,
    ),
    ProviderSetting: st.builds(
        ProviderSetting, key=short, type=st.sampled_from(["bool", "int", "float", "string", "enum", "list"]),
        label=text, default=json_values(3), description=text, options=st.none() | st.lists(short, max_size=3).map(tuple),
        minimum=st.none() | st.floats(allow_nan=False, allow_infinity=False),
        maximum=st.none() | st.floats(allow_nan=False, allow_infinity=False),
        step=st.none() | st.floats(allow_nan=False, allow_infinity=False),
        secret=st.booleans(),
        visible_when=st.none() | st.lists(st.tuples(short, json_values(2)), max_size=2).map(tuple),
        required=st.booleans(),
    ),
    interfaces.StorageStreamResult: st.builds(
        interfaces.StorageStreamResult, file=maybe_text, url=maybe_text, headers=st.dictionaries(short, text, max_size=2),
        chunks=st.none(), media_type=short, filename=text, size=maybe_int, offset=small_int,
    ),
    interfaces.WorkspaceFile: st.builds(interfaces.WorkspaceFile, workspace=text, name=text),
    interfaces.SyncCandidate: st.builds(interfaces.SyncCandidate, sync_id=short, track=tracks),
    interfaces.Identity: st.builds(
        interfaces.Identity, title=text, artist=text, score=st.floats(allow_nan=False, allow_infinity=False),
        recording_id=maybe_text,
    ),
    FilesystemAccess: st.builds(FilesystemAccess, setting=short, mode=st.sampled_from(["ro", "rw"])),
    TcpAccess: st.builds(TcpAccess, setting=short, ports=st.sampled_from(["any", "21", "30000-30100"])),
    ResourceLimits: st.builds(ResourceLimits, memory_mb=maybe_int, open_files=maybe_int, processes=maybe_int),
    Permissions: st.builds(
        Permissions,
        egress=st.lists(short, max_size=3).map(tuple), egress_from_settings=st.lists(short, max_size=3).map(tuple),
        filesystem_from_settings=st.lists(st.builds(FilesystemAccess, setting=short, mode=st.sampled_from(["ro", "rw"])), max_size=2).map(tuple),
        tcp_from_settings=st.lists(st.builds(TcpAccess, setting=short, ports=st.sampled_from(["any", "21", "30000-30100"])), max_size=2).map(tuple),
        limits=st.builds(ResourceLimits, memory_mb=maybe_int, open_files=maybe_int, processes=maybe_int),
    ),
    ContextSnapshot: st.builds(
        ContextSnapshot, settings=json_dicts, http_timeout=small_int, download_timeout=small_int,
        connect_timeout=small_int, stream_start_timeout=small_int, api_prefix=short,
    ),
    manifest.Manifest: st.builds(
        manifest.Manifest, provider_api_version=maybe_int, name=short, label=text,
        roles=st.lists(st.sampled_from(sorted(manifest.ROLES)), max_size=3, unique=True).map(tuple),
        auth=st.builds(auth.AuthDefinition, flow=st.just(auth.AuthFlow.NONE), account_model=st.just(auth.AccountModel.NONE)),
        config_schema=st.lists(
            st.builds(ProviderSetting, key=short, type=st.just("string"), label=text), max_size=2,
        ).map(tuple),
        declarations=st.dictionaries(short, st.booleans() | st.none() | st.integers() | st.frozensets(short, max_size=3), max_size=4),
        implements=st.lists(short, max_size=3).map(tuple),
        permissions=st.builds(Permissions, egress=st.lists(short, max_size=2).map(tuple)),
    ),
}

ENUM_STRATEGIES = {enum: st.sampled_from(list(enum)) for enum in wire.registered_enums().values()}


def same(a, b) -> bool:
    """Equality that also tells a tuple from a list and a naive datetime from an aware one."""
    if type(a) is not type(b):
        return False
    if isinstance(a, datetime):
        return a == b and (a.tzinfo is None) == (b.tzinfo is None) and a.utcoffset() == b.utcoffset()
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    return a == b


def test_every_wire_type_has_a_strategy():
    # The types of the contract (a test may register a type of its own, test_contract_evolution.py's probe).
    missing = [name for name, cls in wire.registered_types().items()
               if cls.__module__.startswith("musizerr_sdk.") and cls not in STRATEGIES]
    assert not missing, f"no round trip strategy for {missing}"
    assert not [cls for cls in STRATEGIES if cls not in wire.registered_types().values()]


@pytest.mark.parametrize("cls", sorted(STRATEGIES, key=lambda c: c.__name__), ids=lambda c: c.__name__)
def test_a_registered_type_survives_a_round_trip_through_json(cls):
    @SETTINGS
    @given(STRATEGIES[cls])
    def check(value):
        encoded = wire.encode(value)
        assert json.loads(json.dumps(encoded, allow_nan=False)) == encoded
        assert wire.decode(encoded) == value
        assert wire.loads(wire.dumps(value)) == value

    check()


@pytest.mark.parametrize("enum", list(ENUM_STRATEGIES), ids=lambda e: e.__name__)
def test_a_registered_enum_survives_a_round_trip(enum):
    @SETTINGS
    @given(ENUM_STRATEGIES[enum])
    def check(member):
        assert wire.loads(wire.dumps(member)) is member

    check()


@SETTINGS
@given(datetimes)
def test_a_datetime_keeps_being_naive_or_aware_with_its_offset(value):
    back = wire.loads(wire.dumps(value))

    assert back == value
    assert (back.tzinfo is None) == (value.tzinfo is None)
    assert back.utcoffset() == value.utcoffset()


@SETTINGS
@given(json_values())
def test_plain_json_values_stay_plain(value):
    assert wire.encode(value) == value
    assert same(wire.loads(wire.dumps(value)), value)


python_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | text | st.binary(max_size=8) | datetimes,
    lambda children: (
        st.lists(children, max_size=3)
        | st.tuples(children, children)
        | st.sets(st.integers() | text, max_size=3)
        | st.frozensets(st.integers() | text, max_size=3)
        | st.dictionaries(text, children, max_size=3)
        | st.dictionaries(st.integers() | st.tuples(text, st.integers()) | st.frozensets(text, max_size=2), children, max_size=3)
    ),
    max_leaves=12,
)


@SETTINGS
@given(python_values)
def test_tuples_sets_bytes_and_odd_keys_come_back_as_what_they_were(value):
    assert same(wire.loads(wire.dumps(value)), value)


def test_a_dict_with_the_tag_key_is_not_taken_for_a_tagged_value():
    value = {"$": "Track", "title": "x"}

    assert wire.loads(wire.dumps(value)) == value
    assert wire.loads(wire.dumps({"outer": value})) == {"outer": value}


@pytest.mark.parametrize("value", [float("inf"), float("-inf")])
def test_infinite_floats_survive(value):
    assert wire.loads(wire.dumps(value)) == value


def test_nan_survives():
    assert math.isnan(wire.loads(wire.dumps(float("nan"))))


def test_a_float_stays_a_float_and_an_int_an_int():
    assert type(wire.loads(wire.dumps(1.0))) is float
    assert type(wire.loads(wire.dumps(1))) is int
    assert type(wire.loads(wire.dumps(True))) is bool


def test_the_encoding_of_a_set_is_deterministic():
    assert wire.dumps({"b", "a", "c"}) == wire.dumps({"c", "a", "b"})


def test_an_unknown_tag_is_an_error():
    with pytest.raises(wire.WireError, match="unknown wire tag"):
        wire.decode({"$": "Nothing", "v": 1})


def test_an_unregistered_dataclass_cannot_be_encoded():
    from dataclasses import dataclass

    @dataclass
    class Stranger:
        x: int = 1

    with pytest.raises(wire.WireError):
        wire.encode(Stranger())


@pytest.mark.parametrize("value", [object(), {1, object()}, lambda: 1, iter([1]), (x for x in [1])])
def test_a_value_of_another_type_cannot_be_encoded(value):
    with pytest.raises(wire.WireError):
        wire.encode(value)


@pytest.mark.parametrize("data", [
    {"$": "Track"},
    {"$": "Track", "title": "x", "nope": 1},
    {"$": "enum", "e": "AuthFlow", "v": "nope"},
    {"$": "enum", "e": "Nope", "v": "x"},
    {"$": "datetime", "v": "yesterday"},
    {"$": "bytes", "v": "###"},
    {"$": "tuple"},
])
def test_malformed_data_is_an_error_not_a_guess(data):
    with pytest.raises(wire.WireError):
        wire.decode(data)


def test_only_json_data_can_be_decoded():
    with pytest.raises(wire.WireError):
        wire.decode((1, 2))


def test_the_stream_of_a_storage_result_is_a_marker_rebuilt_from_the_stream_given():
    result = interfaces.StorageStreamResult(chunks=iter([b"ab", b"cd"]), size=4, media_type="audio/mpeg")

    encoded = wire.encode(result)

    assert encoded["chunks"] == {"$": "chunks"}
    json.dumps(encoded)
    rebuilt = wire.decode(encoded, chunks=iter([b"ab", b"cd"]))
    assert list(rebuilt.chunks) == [b"ab", b"cd"]
    assert (rebuilt.size, rebuilt.media_type) == (4, "audio/mpeg")
    with pytest.raises(wire.WireError, match="byte stream"):
        wire.decode(encoded)


def test_names_of_wire_types_are_unique():
    names = [cls.__name__ for cls in wire.registered_types().values()]

    assert len(names) == len(set(names))


def test_registering_two_types_with_one_name_is_refused():
    from dataclasses import dataclass

    @dataclass
    class Track:  # same name as models.Track
        x: int = 0

    with pytest.raises(ValueError):
        wire.register(Track)


# --- the exceptions of the contract are values -------------------------------

TRACK = models.Track(title="T", artist="A", album="B", duration_ms=1)

CONTRACT_EXCEPTIONS = [
    errors.DownloadDelegated("p", ["queued", {"id": 1}], handle="h"),
    errors.DownloadDeclined("p", "have_release", "already there"),
    errors.DelegateHoldsRelease("p", "h", [TRACK], complete=True, release="Album", details=["x"], state={"k": [1]}),
    errors.RemotePlaylistGone("gone"),
    errors.ProviderRateLimited("slow down", retry_after=7.5),
    errors.ProviderRateLimited("slow down"),
    PermissionError("missing scope"),
    NotImplementedError("nope"),
]


@pytest.mark.parametrize("exc", CONTRACT_EXCEPTIONS, ids=lambda e: f"{type(e).__name__}-{str(e)[:10]}")
def test_the_exceptions_of_the_contract_are_wire_values(exc):
    back = wire.loads(wire.dumps(exc))

    assert type(back) is type(exc) and back.args == exc.args and vars(back) == vars(exc)


def test_delegate_holds_release_is_also_an_argument_of_a_call():
    held = errors.DelegateHoldsRelease("p", "h", [TRACK], complete=False, release="R", details=[1], state={"s": 1})

    back = wire.loads(wire.dumps({"args": [held]}))["args"][0]

    assert (back.handle, back.tracks, back.complete, back.state) == ("h", [TRACK], False, {"s": 1})


def test_every_contract_exception_is_registered():
    assert set(wire.registered_exceptions()) == {
        "DownloadDelegated", "DownloadDeclined", "DelegateHoldsRelease", "RemotePlaylistGone",
        "ProviderRateLimited", "PermissionError", "NotImplementedError",
    }


def test_a_subclass_of_a_contract_exception_travels_as_its_ancestor_and_another_exception_does_not_travel():
    class Quota(errors.ProviderRateLimited): ...

    assert type(wire.loads(wire.dumps(Quota("q", retry_after=3)))) is errors.ProviderRateLimited
    assert wire.exception_type(RuntimeError("x")) is None
    with pytest.raises(wire.WireError):
        wire.encode(RuntimeError("x"))
