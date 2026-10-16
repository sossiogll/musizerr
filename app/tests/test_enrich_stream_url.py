"""
Enrichment dello stream_url dal sync provider: correttezza, caching TTL e
skip degli item già risolti (il pezzo caldo della latenza di ricerca).
"""

from __future__ import annotations

from core.identifiers import sync_id_type
from musizerr_sdk.identifiers import Identifier
from core.serialization import serialize_track
from services.enrichment import enrich_dicts_with_stream_url, enrich_tracks_with_stream_url

from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider, make_track


def test_sets_stream_url_only_for_available_tracks():
    sync = FakeSyncProvider(available={"song a": "sid-1"})
    registry = FakeRegistry(syncs=[sync])
    tracks = [make_track("Song A"), make_track("Song B")]

    enrich_tracks_with_stream_url(tracks, registry, FakeAuthManager())

    assert tracks[0].stream_url == f"/api/stream/provider/{sync.name}/sid-1"
    assert tracks[1].stream_url is None


def test_availability_lookups_are_cached():
    sync = FakeSyncProvider(available={"song a": "sid-1"})
    registry = FakeRegistry(syncs=[sync])
    am = FakeAuthManager()

    enrich_tracks_with_stream_url([make_track("Song A")], registry, am)
    first = sync.availability_calls
    assert first >= 1

    # Seconda passata con lo stesso titolo: servita dalla cache, niente rete.
    fresh = make_track("Song A")
    enrich_tracks_with_stream_url([fresh], registry, am)
    assert sync.availability_calls == first
    assert fresh.stream_url == f"/api/stream/provider/{sync.name}/sid-1"


def test_skips_tracks_that_already_carry_their_sync_id():
    sync = FakeSyncProvider(available={"song a": "sid-other"})
    registry = FakeRegistry(syncs=[sync])
    t = make_track("Song A")
    t.add_id(Identifier(type=sync_id_type(sync.name), value="sid-1"))

    enrich_tracks_with_stream_url([t], registry, FakeAuthManager())

    assert sync.availability_calls == 0    # nemmeno interrogato
    assert t.stream_url.endswith("/sid-1")


def test_a_stream_url_of_another_provider_does_not_skip_the_lookup():
    # Prima bastava uno stream_url qualunque (es. quello salvato di un like
    # importato da un altro servizio) per saltare il controllo nel sync
    # provider: la traccia in libreria restava "piaciuta altrove".
    sync = FakeSyncProvider(available={"song a": "sid-1"})
    registry = FakeRegistry(syncs=[sync])
    t = make_track("Song A")
    t.stream_url = "/preset"

    enrich_tracks_with_stream_url([t], registry, FakeAuthManager())

    assert sync.availability_calls == 1
    assert any(i.storage_type() == sync_id_type(sync.name) for i in t.external_ids)


def test_no_sync_providers_is_a_noop():
    registry = FakeRegistry(syncs=[])
    tracks = [make_track("Song A")]
    enrich_tracks_with_stream_url(tracks, registry, FakeAuthManager())
    assert tracks[0].stream_url is None


def test_negative_result_is_cached_too():
    sync = FakeSyncProvider(available={})  # niente disponibile
    registry = FakeRegistry(syncs=[sync])
    am = FakeAuthManager()

    enrich_tracks_with_stream_url([make_track("Ghost")], registry, am)
    calls_after_first = sync.availability_calls

    enrich_tracks_with_stream_url([make_track("Ghost")], registry, am)
    # Anche il "non disponibile" è in cache: nessuna nuova chiamata.
    assert sync.availability_calls == calls_after_first


# ------------------------------------------------------------------
# Bug A (open items): un hit sync deve comparire anche in providers/
# external_ids, non solo in stream_url — il frontend mostra i badge da
# track.providers (ProviderBadges) e la UI di libreria distingue le fonti da
# external_ids. Prima della fix, la Track risultava "servita" solo dal
# metadata provider di origine.
# ------------------------------------------------------------------

def test_sync_hit_adds_provider_and_identifier_on_track_object():
    sync = FakeSyncProvider(available={"song a": "sid-1"})
    registry = FakeRegistry(syncs=[sync])
    track = make_track("Song A")

    enrich_tracks_with_stream_url([track], registry, FakeAuthManager())

    assert sync.name in track.providers
    ident = track.get_id(f"{sync.name}_sync_id")
    assert ident is not None
    assert ident.value == "sid-1"


def test_sync_hit_does_not_duplicate_provider_already_present():
    sync = FakeSyncProvider(available={"song a": "sid-1"})
    registry = FakeRegistry(syncs=[sync])
    track = make_track("Song A")
    track.providers.append(sync.name)

    enrich_tracks_with_stream_url([track], registry, FakeAuthManager())

    assert track.providers.count(sync.name) == 1


def test_sync_hit_adds_provider_and_identifier_on_dict_variant():
    sync = FakeSyncProvider(available={"song a": "sid-1"})
    registry = FakeRegistry(syncs=[sync])
    d = serialize_track(make_track("Song A"))

    enrich_dicts_with_stream_url([d], registry, FakeAuthManager())

    assert sync.name in d["providers"]
    assert {"type": f"{sync.name}_sync_id", "value": "sid-1"} in d["external_ids"]


def test_sync_hit_from_cache_still_records_provider_and_identifier():
    sync = FakeSyncProvider(available={"song a": "sid-1"})
    registry = FakeRegistry(syncs=[sync])
    am = FakeAuthManager()

    # Prima chiamata: popola la cache di disponibilità.
    enrich_tracks_with_stream_url([make_track("Song A")], registry, am)

    # Seconda traccia, stesso titolo: risolta dalla cache (niente rete), ma
    # deve comunque ricevere provider/identifier — non solo lo stream_url.
    fresh = make_track("Song A")
    enrich_tracks_with_stream_url([fresh], registry, am)

    assert sync.name in fresh.providers
    assert fresh.get_id(f"{sync.name}_sync_id") is not None
