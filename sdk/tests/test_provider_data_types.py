"""
The contract types a provider returns INSTEAD of deciding: the host judges
(``SyncCandidate`` for a sync library, ``DelegateHoldsRelease`` for a delegate).
"""
from __future__ import annotations

import dataclasses

import pytest

from musizeer_sdk.errors import DelegateHoldsRelease
from musizeer_sdk.interfaces import DownloadProvider, SyncCandidate, SyncProvider
from musizeer_sdk.models import Track


def test_a_sync_candidate_carries_its_sync_id_explicitly_and_is_immutable():
    candidate = SyncCandidate(sync_id="s1", track=Track(title="T", artist="A", album="B", duration_ms=1, provider_id="other"))

    assert candidate.sync_id == "s1" and candidate.track.provider_id == "other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.sync_id = "s2"  # type: ignore[misc]


def test_a_sync_provider_returns_candidates_and_has_no_verdict_method():
    assert "find_candidates" in SyncProvider.__abstractmethods__
    assert not hasattr(SyncProvider, "is_available")


def test_a_delegate_reports_what_it_holds_as_data():
    held = DelegateHoldsRelease("p", "42", [], complete=False, release="Album", details=[{"k": 1}], state={"m": True})

    assert (held.provider, held.handle, held.tracks, held.complete, held.release) == ("p", "42", [], False, "Album")
    assert held.details == [{"k": 1}] and held.state == {"m": True}
    assert DelegateHoldsRelease("p", "1", [], complete=True, release="R").details == []


def test_the_optional_delegation_methods_default_to_nothing():
    assert DownloadProvider.list_delegated_tracks(None, "h", None) == []  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        DownloadProvider.search_delegated(None, held=None, session=None)  # type: ignore[arg-type]
