"""
Whether a track is in a sync provider's library is the HOST's decision
(``services.enrichment.check_sync_availability``): the provider searches its own
API and returns the CANDIDATES it found, each with its sync id
(``SyncProvider.find_candidates``); the host matches them with its matcher.
Generic fakes only.
"""
from __future__ import annotations

import pytest

from core.interfaces import SyncAvailability
from musizeer_sdk.interfaces import SyncCandidate
from services.enrichment import check_sync_availability

from tests.conftest import FakeSyncProvider, make_track
from tests.loopback import as_proxy
from services.provider_proxy import ProviderCallFailed

WANTED = make_track(title="Wanted Song", artist="Artist X", album="Album X", duration_ms=200_000)


class _Library(FakeSyncProvider):
    def __init__(self, candidates) -> None:
        super().__init__("library")
        self.candidates = list(candidates)

    def find_candidates(self, track, session):
        return list(self.candidates)


def _candidate(sync_id: str, **fields) -> SyncCandidate:
    # The track deliberately carries ANOTHER provider id: the sync id is the
    # candidate's, never read from the track.
    return SyncCandidate(sync_id=sync_id, track=make_track(provider_id="not-the-sync-id", **fields))


def test_the_sync_id_is_the_one_of_the_candidate_the_host_matched():
    library = _Library([
        _candidate("s1", title="Other Song", artist="Artist X", album="Album X"),
        _candidate("s2", title="Wanted Song", artist="Artist X", album="Album X"),
    ])

    assert check_sync_availability(as_proxy(library), WANTED, None) == SyncAvailability(sync_id="s2")


def test_the_first_matching_candidate_wins():
    library = _Library([
        _candidate("first", title="Wanted Song", artist="Artist X", album="Album X"),
        _candidate("second", title="Wanted Song", artist="Artist X", album="Album X"),
    ])

    assert check_sync_availability(as_proxy(library), WANTED, None).sync_id == "first"


def test_a_candidate_of_another_version_is_not_the_track():
    library = _Library([_candidate("remix", title="Wanted Song (Club Remix)", artist="Artist X", album="Album X")])

    assert check_sync_availability(as_proxy(library), WANTED, None) is None


def test_no_candidate_is_not_available():
    assert check_sync_availability(as_proxy(_Library([])), WANTED, None) is None


def test_a_provider_that_cannot_search_raises_it_is_not_an_absence():
    class _Down(_Library):
        def find_candidates(self, track, session):
            raise ConnectionError("down")

    with pytest.raises(ProviderCallFailed) as failed:
        check_sync_availability(as_proxy(_Down([])), WANTED, None)
    assert failed.value.kind == "unreachable"
