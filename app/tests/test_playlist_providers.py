"""
What the playlists ask of the providers, in the contract: the optional
playlist methods of a sync provider (default: not supported), the version
marker and the "tracks only for your own playlists" declaration of a metadata
provider (default: none), and the capabilities the host names from what a
provider declares. How a real sync provider talks to its server, and a real
catalog reads its marker, is tested in their own repositories.
"""
from __future__ import annotations

import pytest

from musizeer_sdk.auth import AuthSession
from core.interfaces import metadata_capabilities, personal_capabilities

from tests.conftest import FakeMetadataProvider, FakeSyncProvider
from tests.playlists_support import PlaylistSyncProvider, Source
from tests.loopback import as_proxy

SESSION = AuthSession.anonymous()


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_a_sync_provider_keeps_no_playlists_unless_it_says_so():
    plain = FakeSyncProvider("plain")

    assert plain.supports_playlists() is False
    for call in (lambda: plain.create_playlist("n", [], SESSION), lambda: plain.update_playlist("r", "n", [], SESSION),
                 lambda: plain.delete_playlist("r", SESSION)):
        with pytest.raises(NotImplementedError):
            call()


def test_a_metadata_provider_has_no_marker_and_no_restriction_unless_it_says_so():
    plain = FakeMetadataProvider("plain")

    assert plain.get_playlist_snapshot("p", SESSION) is None
    assert plain.restricts_playlist_tracks() is False
    assert "playlist_tracks_restricted" not in metadata_capabilities(as_proxy(plain))


def test_the_capabilities_name_what_a_provider_declares():
    assert metadata_capabilities(as_proxy(Source())) == ["playlist_tracks_restricted"]
    assert personal_capabilities(as_proxy(PlaylistSyncProvider("kept"))) == ["propagate_playlists"]

