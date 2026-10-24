"""
Additions within a contract major never break a peer built on another minor (ADR-0007 §3.1).

A field added after the first release of the major carries the minor it exists since and a default; the
codec ignores the fields it does not know and fills the ones it misses; each side of a call encodes for the
other's minor. ``Probe`` stands for a contract type that gained two fields, in 6.1 and in 6.3; ``OldProbe``
for the same type as an SDK of 6.0 knows it, decoded the way a peer of an older minor decodes: strictly.
"""
from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import musizeer_sdk
from musizeer_sdk import labels, manifest as manifest_module, protocol, wire
from musizeer_sdk.runner import ProviderRunner
from musizeer_sdk.version import BASELINE, SDK_VERSION, minor_of, peer_minor

from sample_providers import SampleMetadata, session


@wire.register
@dataclass(frozen=True)
class EvolutionProbe:
    title: str
    year: int | None = None
    mood: str | None = field(default=None, metadata={wire.SINCE: "6.1"})
    tempo: int = field(default=0, metadata={wire.SINCE: "6.3"})


@dataclass(frozen=True)
class OldProbe:
    """The same type, as an SDK of 6.0 knows it."""

    title: str
    year: int | None = None


def strict_decode_as_old(data: dict) -> OldProbe:
    """How a peer of an older minor decodes: every key is a keyword of the class, an unknown one fails."""
    return OldProbe(**{key: value for key, value in data.items() if key != wire.TAG})


PROBE = EvolutionProbe("Song", 1999, mood="calm", tempo=120)


# ---- the codec ------------------------------------------------------------------------------------

def test_without_a_peer_every_field_travels():
    assert set(wire.encode(PROBE)) == {"$", "title", "year", "mood", "tempo"}


@pytest.mark.parametrize("peer, fields", [
    ((6, 0), {"title", "year"}),
    ((6, 1), {"title", "year", "mood"}),
    ((6, 3), {"title", "year", "mood", "tempo"}),
    ((6, 9), {"title", "year", "mood", "tempo"}),
])
def test_a_peer_gets_only_the_fields_of_its_minor(peer, fields):
    assert set(wire.encode(PROBE, peer)) - {"$"} == fields


def test_the_rule_holds_inside_lists_dicts_and_tuples():
    encoded = wire.encode({"probes": [PROBE], "pair": (PROBE, 1)}, (6, 0))
    assert "mood" not in encoded["probes"][0] and "mood" not in encoded["pair"]["v"][0]


def test_what_is_encoded_for_an_old_peer_is_decoded_by_its_strict_codec():
    assert strict_decode_as_old(wire.encode(PROBE, BASELINE)) == OldProbe("Song", 1999)
    with pytest.raises(TypeError):  # without the peer's minor, the old peer would fail
        strict_decode_as_old(wire.encode(PROBE))


def test_a_field_of_a_newer_peer_is_ignored():
    assert wire.decode({"$": "EvolutionProbe", "title": "Song", "from_the_future": [1, 2]}) == EvolutionProbe("Song")


def test_a_field_an_older_peer_does_not_send_gets_its_default():
    assert wire.decode({"$": "EvolutionProbe", "title": "Song", "year": 2001}) == EvolutionProbe("Song", 2001)


def test_a_required_field_is_still_required():
    with pytest.raises(wire.WireError):
        wire.decode({"$": "EvolutionProbe", "year": 2001})


def test_a_field_added_later_must_have_a_default():
    @dataclass
    class NoDefault:
        a: int
        b: int = field(metadata={wire.SINCE: "6.1"})

    with pytest.raises(TypeError, match="needs a default"):
        wire.register(NoDefault)


def test_since_names_an_sdk_minor():
    @dataclass
    class BadSince:
        a: int = field(default=0, metadata={wire.SINCE: "soon"})

    with pytest.raises(TypeError, match="not an SDK minor"):
        wire.register(BadSince)


def test_every_field_marked_in_the_contract_has_a_default_and_a_minor_of_this_major():
    for name, cls in wire.registered_types().items():
        if not cls.__module__.startswith("musizeer_sdk."):
            continue  # a test's own type (the probe above)
        for f in dataclasses.fields(cls):
            if wire.SINCE in f.metadata:
                minor = minor_of(f.metadata[wire.SINCE])
                assert minor is not None and minor[0] == musizeer_sdk.interfaces.PROVIDER_API_VERSION, f"{name}.{f.name}"
                assert minor <= minor_of(SDK_VERSION), f"{name}.{f.name} is newer than this SDK"


# ---- the versions -------------------------------------------------------------------------------------

def test_the_sdk_version_is_the_package_version():
    pyproject = Path(next(iter(musizeer_sdk.__path__))).parent.parent / "pyproject.toml"
    assert tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"] == SDK_VERSION


@pytest.mark.parametrize("text, minor", [("6.1.0", (6, 1)), ("6.1", (6, 1)), ("6.12.3", (6, 12)), (None, BASELINE), ("x", BASELINE)])
def test_a_peer_that_says_nothing_usable_is_the_baseline(text, minor):
    assert peer_minor(text) == minor


# ---- the protocol and the runner ----------------------------------------------------------------------

class DeclaredMetadata(SampleMetadata):
    provider_api_version = musizeer_sdk.interfaces.PROVIDER_API_VERSION


class ProbeMetadata(SampleMetadata):
    def search_tracks(self, query, session, limit=20):
        return [PROBE]


def _request(sdk: str | None) -> protocol.Frame:
    message = {"v": protocol.PROTOCOL_VERSION, "op": protocol.OP_CALL, "method": "search_tracks",
               "args": ["q"], "kwargs": {}, "session": wire.encode(session()), "session_at": None, "context": None}
    if sdk is not None:
        message["sdk"] = sdk
    return protocol.json_frame(message)


def _result_fields(sdk: str | None) -> set[str]:
    frame = next(iter(ProviderRunner(ProbeMetadata).handle(_request(sdk))))
    return set(protocol.load_message(frame)["value"][0]) - {"$"}


def test_a_request_says_the_hosts_sdk_and_is_encoded_for_the_providers():
    message = protocol.load_message(protocol.call_request("search_tracks", ("q", PROBE), {}, None, (6, 0)))
    assert message["sdk"] == SDK_VERSION
    assert set(message["args"][1]) - {"$"} == {"title", "year"}


def test_the_runner_answers_each_host_in_its_own_minor():
    assert _result_fields(None) == {"title", "year"}  # a host that says nothing is the baseline (6.0)
    assert _result_fields("6.1.0") == {"title", "year", "mood"}
    assert _result_fields("6.3.2") == {"title", "year", "mood", "tempo"}


# ---- the manifest and the image ------------------------------------------------------------------------

def test_a_provider_says_the_sdk_it_was_built_on():
    built = manifest_module.build(DeclaredMetadata(None))
    assert built.sdk_version == SDK_VERSION
    assert labels.labels_for(built)[labels.LABEL_SDK_VERSION] == SDK_VERSION
    assert wire.encode(built, BASELINE)["sdk_version"] == SDK_VERSION, "the field is part of the base of the major"


def test_a_manifest_that_says_nothing_is_the_baseline():
    silent = dataclasses.replace(manifest_module.build(SampleMetadata(None)), sdk_version=None)
    decoded = wire.decode(wire.encode(silent, BASELINE))
    assert decoded.sdk_version is None
    assert peer_minor(decoded.sdk_version) == BASELINE
