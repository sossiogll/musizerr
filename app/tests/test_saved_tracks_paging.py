"""
``MetadataProvider.iter_saved_tracks``: the host reads a provider's likes page
by page through it. The default pages through ``get_saved_tracks`` by offset
and ends at an empty or short page (what the host always did); a provider that
overrides it decides where its library ends.
"""
from __future__ import annotations

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from workers import tasks

from tests.conftest import FakeMetadataProvider, make_track
from tests.loopback import as_proxy


class _Offsets(FakeMetadataProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, total: int):
        super().__init__("offsets")
        self.total, self.asked = total, []

    def get_saved_tracks(self, session, limit=50, offset=0):
        self.asked.append((limit, offset))
        return [make_track(f"Song {n}") for n in range(offset, min(offset + limit, self.total))]


def test_the_default_pages_by_offset_until_a_short_page():
    provider = _Offsets(120)

    pages = list(as_proxy(provider).iter_saved_tracks(AuthSession.anonymous()))

    assert [len(p) for p in pages] == [50, 50, 20]
    assert provider.asked == [(50, 0), (50, 50), (50, 100)]


def test_the_default_asks_once_more_after_a_full_last_page_and_stops_at_the_empty_one():
    provider = _Offsets(100)

    pages = list(as_proxy(provider).iter_saved_tracks(AuthSession.anonymous()))

    assert [len(p) for p in pages] == [50, 50]
    assert provider.asked == [(50, 0), (50, 50), (50, 100)]


def test_the_default_of_an_empty_library_yields_nothing():
    assert list(as_proxy(_Offsets(0)).iter_saved_tracks(AuthSession.anonymous())) == []


def test_the_host_collects_every_page_and_reports_each():
    seen: list[int] = []

    tracks = tasks._fetch_all_saved_tracks(as_proxy(_Offsets(120)), AuthSession.anonymous(), on_page=seen.append)

    assert len(tracks) == 120 and seen == [50, 100, 120]


def test_the_host_follows_the_provider_when_it_pages_by_itself():
    class _Cursor(_Offsets):
        def iter_saved_tracks(self, session, page_size=50):
            yield [make_track("a")]
            yield []  # a page with nothing to keep is not the end
            yield [make_track("b"), make_track("c")]

    assert [t.title for t in tasks._fetch_all_saved_tracks(as_proxy(_Cursor(0)), AuthSession.anonymous())] == ["a", "b", "c"]
