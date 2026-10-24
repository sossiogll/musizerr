"""
Whether the track a download was requested for is already in a delegated
package is the HOST's decision (``services.delegation.poll_for_requested_track``):
a delegate only lists the tracks of the package it has a file for
(``list_delegated_tracks``) and answers for the package as a whole
(``poll_delegation``). Generic fakes only: no real provider here.
"""
from __future__ import annotations

import pytest

from core import download_states as ds
from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizeer_sdk.delegation import DelegationStatus
from services.delegation import poll_for_requested_track

from tests.conftest import FakeDownloadProvider, FakeRegistry, make_track
from tests.loopback import as_proxy
from services.provider_proxy import ProviderCallFailed


class _Delegate(FakeDownloadProvider):
    """Lists tracks and says the package is still downloading, whatever it lists."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, present=(), *, fail_listing: bool = False) -> None:
        super().__init__("delegate")
        self.present = list(present)
        self.fail_listing = fail_listing
        self.listed = 0
        self.polled = 0

    def list_delegated_tracks(self, handle, session):
        self.listed += 1
        if self.fail_listing:
            raise ConnectionError("unreachable")
        return list(self.present)

    def poll_delegation(self, handle, session):
        self.polled += 1
        return DelegationStatus(state=ds.DOWNLOADING)


REQUESTED = make_track(title="Requested Song", artist="Artist X", album="Album X", duration_ms=200_000)


def test_a_delegate_that_lists_the_track_without_deciding_is_judged_by_the_host():
    delegate = _Delegate([make_track(title="Requested Song", artist="Artist X", album="", duration_ms=200_000)])

    status = poll_for_requested_track(as_proxy(delegate), "h", None, REQUESTED)

    assert status == DelegationStatus(state=ds.DOWNLOADED)
    assert delegate.polled == 0, "the file of the track decides before any signal about the package"


def test_a_track_with_a_drifting_duration_is_still_the_requested_one_when_its_title_is_unique():
    delegate = _Delegate([make_track(title="Requested Song", artist="Artist X", album="", duration_ms=241_000)])

    assert poll_for_requested_track(as_proxy(delegate), "h", None, REQUESTED).state == ds.DOWNLOADED


def test_another_track_of_the_package_is_not_the_requested_one():
    delegate = _Delegate([make_track(title="Another Song", artist="Artist X", album="", duration_ms=200_000)])

    status = poll_for_requested_track(as_proxy(delegate), "h", None, REQUESTED)

    assert status.state == ds.DOWNLOADING
    assert delegate.polled == 1


def test_a_package_with_nothing_listed_is_judged_by_its_own_status():
    delegate = _Delegate()

    assert poll_for_requested_track(as_proxy(delegate), "h", None, REQUESTED).state == ds.DOWNLOADING


def test_without_a_requested_track_the_package_status_is_all_there_is():
    delegate = _Delegate([make_track(title="Requested Song", artist="Artist X", album="")])

    status = poll_for_requested_track(as_proxy(delegate), "h", None, None)

    assert status.state == ds.DOWNLOADING
    assert delegate.listed == 0


def test_a_delegate_that_cannot_answer_raises_to_the_caller():
    delegate = _Delegate(fail_listing=True)

    with pytest.raises(ProviderCallFailed) as failed:
        poll_for_requested_track(as_proxy(delegate), "h", None, REQUESTED)
    assert (failed.value.kind, failed.value.exception_type) == ("unreachable", "ConnectionError")
    assert delegate.polled == 0


def test_the_listed_tracks_are_not_modified_when_the_host_fills_the_album():
    listed = make_track(title="Requested Song", artist="Artist X", album="", duration_ms=200_000)
    delegate = _Delegate([listed])

    poll_for_requested_track(as_proxy(delegate), "h", None, REQUESTED)

    assert listed.album == ""


# ---------------------------------------------------------------------------
# A delegate that already holds the release reports it; the host decides
# ---------------------------------------------------------------------------

from core.download_resolver import ALREADY_IN_DELEGATE, DownloadResolver, download_fuzzy  # noqa: E402
from musizeer_sdk.errors import DelegateHoldsRelease, DownloadDeclined, DownloadDelegated  # noqa: E402


class _Holder(FakeDownloadProvider):
    """A fuzzy delegate whose system already has the release: it reports what it
    holds and never says what that means for the requested track."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    supports_fuzzy = True

    def __init__(self, tracks, *, complete: bool) -> None:
        super().__init__("holder")
        self.tracks = list(tracks)
        self.complete = complete
        self.searched: list[DelegateHoldsRelease] = []

    def download_fuzzy(self, track, workspace, session):
        raise DelegateHoldsRelease(
            self.name, "42", self.tracks, complete=self.complete, release="Album X",
            details=[{"status": "held"}], state={"monitored": False},
        )

    def search_delegated(self, held, session):
        self.searched.append(held)
        raise DownloadDelegated(self.name, [{"status": "searched"}], held.handle)


def _held(title: str):
    return make_track(title=title, artist="Artist X", album="", duration_ms=200_000)


def test_the_host_delegates_without_a_search_when_the_release_has_the_track():
    holder = _Holder([_held("Requested Song")], complete=False)

    with pytest.raises(DownloadDelegated) as delegated:
        download_fuzzy(as_proxy(holder), REQUESTED, "/unused", None)

    assert (delegated.value.handle, delegated.value.details) == ("42", [{"status": "held"}])
    assert holder.searched == []


def test_the_host_declines_a_track_missing_from_a_complete_release():
    holder = _Holder([_held("Another Song")], complete=True)

    with pytest.raises(DownloadDeclined) as declined:
        download_fuzzy(as_proxy(holder), REQUESTED, "/unused", None)

    assert declined.value.code == ALREADY_IN_DELEGATE
    assert "Album X" in declined.value.message and "Requested Song" in declined.value.message
    assert holder.searched == []


def test_the_host_asks_the_delegate_to_search_when_the_release_can_still_get_the_track():
    holder = _Holder([_held("Another Song")], complete=False)

    with pytest.raises(DownloadDelegated) as delegated:
        download_fuzzy(as_proxy(holder), REQUESTED, "/unused", None)

    assert delegated.value.details == [{"status": "searched"}]
    assert [h.state for h in holder.searched] == [{"monitored": False}]


def test_the_resolver_reports_the_declined_track_with_its_code(tmp_path):
    holder = _Holder([_held("Another Song")], complete=True)
    resolver = DownloadResolver(FakeRegistry(downloads=[holder]), _NoAuth(), ["holder"])

    result = resolver.resolve(REQUESTED, lambda name: str(tmp_path))

    assert not result.success and result.error_code == ALREADY_IN_DELEGATE


class _NoAuth:
    def authenticate(self, provider):
        return None
