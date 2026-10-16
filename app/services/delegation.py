"""
The host's reading of a delegated download. A delegate (a library manager that
downloads whole albums) answers about its PACKAGE: ``poll_delegation`` says how
the album is doing, ``list_delegated_tracks`` lists the tracks it already has a
file for. Whether the track a ``Download`` was asked for is one of them, and
so is already downloaded even if the rest of the package is incomplete or
failed, is the host's decision, made here with the host's matcher.
"""
from __future__ import annotations

from core.matching import requested_track_in_package
from musizerr_sdk.delegation import DOWNLOADED, DelegationStatus
from musizerr_sdk.interfaces import DownloadProvider
from musizerr_sdk.models import Track


def poll_for_requested_track(
    provider: DownloadProvider, handle: str, session, track: Track | None,
) -> DelegationStatus | None:
    """The status of the delegated download ``handle`` for the track it was
    requested for. When the package already has a file for ``track`` (the
    tracks of ``provider.list_delegated_tracks``, paired the way the tracks of
    two editions of one album are, ``requested_track_in_package``) the answer is
    DOWNLOADED, before any signal about the package as a whole; otherwise it is
    what ``provider.poll_delegation`` says. Without a ``track`` it is always
    the latter. Whatever the provider raises (the system did not answer)
    propagates: the caller treats it as transient."""
    if track is not None:
        if requested_track_in_package(track, provider.list_delegated_tracks(handle, session)):
            return DelegationStatus(state=DOWNLOADED)
    return provider.poll_delegation(handle, session)
