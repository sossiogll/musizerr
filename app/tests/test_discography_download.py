"""
Downloading an artist's whole discography (``POST /download/artist``,
``services/discography.py``): the releases of the artist's own types gathered
from ONE provider that declares the ``discography`` capability, the editions of
a release merged, the same song once (the album's copy), a failing release
reported instead of fatal, the cap, the refusals, and the approval policy
counting what the gathered tracks need. With fake providers: no provider name
of the product appears here.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core import download_policy as dp
from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from core.discography import OWN_RELEASE_TYPES, RELEASES_LISTING_LIMIT, distinct_songs, group_releases, select_releases
from core.interfaces import metadata_capabilities
from musizerr_sdk.interfaces import MetadataProvider
from musizerr_sdk.capabilities import CAPABILITY_DISCOGRAPHY
from core.matching import same_song
from musizerr_sdk.models import Album, Artist
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.errors import ProviderRateLimited
from db.models import TrackRecord
from db.settings_store import SettingsRepository
from services import discography

from tests.conftest import FakeMetadataProvider, make_track
from tests.loopback import as_proxy
from tests.download_requests_support import Harness, downloads_of, requests_of, seed_in_library

ARTIST = "band"


def song(title: str, album: str, *, duration_ms: int = 200_000, isrc: str | None = None, pid: str | None = None):
    return make_track(
        title, "Band", album, duration_ms=duration_ms, provider="shelf", provider_id=pid or f"{album}/{title}", isrc=isrc,
    )


def release(title: str, album_type: str | None, songs: list, *, year: int = 2020, pid: str | None = None) -> Album:
    album = Album(
        title=title, artist="Band", year=year, track_count=len(songs), provider="shelf", provider_id=pid or title,
        album_type=album_type,
    )
    album.tracks = list(songs)
    return album


class Shelf(MetadataProvider):
    """A catalog of releases (``supports_discography``) that records what it is asked."""

    name = "shelf"
    label = "Shelf"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(
        self, releases: list[Album], *, supported: bool = True, failing: dict | None = None,
        unreadable: list[Album] | None = None,
    ):
        super().__init__(StaticProviderContext())
        self.releases = releases
        self.supported = supported
        # Releases whose read raises, by id; and releases it lists but cannot give.
        self.failing = failing or {}
        self.unreadable = unreadable or []
        self.read: list[str] = []
        # The limit of every listing it was asked for.
        self.asked: list[int] = []

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def supports_discography(self):
        return self.supported

    def get_artist(self, provider_id, session):
        return Artist(name="Band", provider=self.name, provider_id=provider_id)

    def get_artist_albums(self, provider_id, session, limit=50):
        """The releases as a listing gives them: without their tracks. Like a real
        provider it pages inside, so it gives as many as it is asked for."""
        self.asked.append(limit)
        return [
            Album(title=r.title, artist=r.artist, year=r.year, track_count=r.track_count, provider=self.name,
                  provider_id=r.provider_id, album_type=r.album_type)
            for r in [*self.releases, *self.unreadable]
        ][:limit]

    def get_album(self, provider_id, session):
        self.read.append(provider_id)
        if provider_id in self.failing:
            raise self.failing[provider_id]
        return next((r for r in self.releases if r.provider_id == provider_id), None)


def catalog() -> list[Album]:
    """An artist with every kind of release; the single repeats a song of the album."""
    return [
        release("First Album", "album", [song("Opening", "First Album", isrc="I1"), song("Middle", "First Album", isrc="I2"),
                                         song("Closing", "First Album", isrc="I3")], year=2015),
        release("Opening", "single", [song("Opening", "Opening", isrc="I1", pid="single/Opening"),
                                      song("B Side", "Opening")], year=2014),
        release("Short EP", "ep", [song("Only Here", "Short EP")], year=2016),
        release("Greatest Hits", "compilation", [song("Hit Only", "Greatest Hits")], year=2018),
        release("Guest Spot", "appears_on", [song("Feature", "Guest Spot")], year=2019),
    ]


def titles(engine) -> list[str]:
    with Session(engine) as session:
        return sorted(session.exec(select(TrackRecord.title)).all())


def harness(engine, monkeypatch, shelf: Shelf, **kwargs) -> Harness:
    return Harness(engine, monkeypatch, metadata=[shelf], **kwargs)


def download(h: Harness, who: str = "admin", **body):
    return h.call(who, "POST", "/download/artist", json={"provider": "shelf", "id": ARTIST, **body}, doc="/download/artist")


# ---------------------------------------------------------------------------
# What is gathered
# ---------------------------------------------------------------------------


def test_the_artists_own_releases_are_gathered_and_the_others_left_out(engine, monkeypatch):
    shelf = Shelf(catalog())
    h = harness(engine, monkeypatch, shelf)

    resp = download(h)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Album, EP and single — not the compilation nor the release of someone else.
    assert titles(engine) == ["B Side", "Closing", "Middle", "Only Here", "Opening"]
    assert (body["total_tracks"], body["skipped"], body["releases"], body["releases_failed"], body["provider_errors"]) == (
        5, 0, 3, 0, [],
    )
    assert sorted(shelf.read) == ["First Album", "Opening", "Short EP"]
    assert len(h.queued) == 5


def test_the_release_types_can_be_asked_for(engine, monkeypatch):
    shelf = Shelf(catalog())
    h = harness(engine, monkeypatch, shelf)

    resp = download(h, release_types=["compilation", "appears_on"])

    assert resp.status_code == 200, resp.text
    assert titles(engine) == ["Feature", "Hit Only"]
    assert sorted(shelf.read) == ["Greatest Hits", "Guest Spot"]


def test_every_type_can_be_included(engine, monkeypatch):
    h = harness(engine, monkeypatch, Shelf(catalog()))

    resp = download(h, release_types=["album", "single", "ep", "compilation", "live", "appears_on"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["releases"] == 5


def test_the_editions_of_a_release_are_read_once_through_the_standard_one(engine, monkeypatch):
    standard = release("First Album", "album", [song("Opening", "First Album"), song("Closing", "First Album")], pid="std")
    deluxe = release("First Album (Deluxe Edition)", "album", [song("Opening", "First Album (Deluxe Edition)"),
                                                               song("Closing", "First Album (Deluxe Edition)"),
                                                               song("Bonus", "First Album (Deluxe Edition)")], pid="dlx")
    shelf = Shelf([deluxe, standard])
    h = harness(engine, monkeypatch, shelf)

    resp = download(h)

    assert resp.status_code == 200, resp.text
    assert shelf.read == ["std"]
    assert resp.json()["releases"] == 1 and titles(engine) == ["Closing", "Opening"]


def test_the_album_keeps_its_version_of_a_song_the_single_repeats(engine, monkeypatch):
    shelf = Shelf(catalog())
    h = harness(engine, monkeypatch, shelf)

    download(h)

    with Session(engine) as session:
        opening = session.exec(select(TrackRecord).where(TrackRecord.title == "Opening")).all()
    assert [t.album for t in opening] == ["First Album"]


def test_an_untyped_release_counts_as_an_album(engine, monkeypatch):
    shelf = Shelf([release("Plain", None, [song("One", "Plain")])])
    h = harness(engine, monkeypatch, shelf)

    resp = download(h, release_types=["album"])

    assert resp.status_code == 200 and titles(engine) == ["One"]


# ---------------------------------------------------------------------------
# Failures and limits
# ---------------------------------------------------------------------------


def test_a_release_that_fails_is_reported_and_does_not_stop_the_others(engine, monkeypatch):
    shelf = Shelf(catalog(), failing={"Short EP": RuntimeError("500 internal server error")})
    h = harness(engine, monkeypatch, shelf)

    resp = download(h)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert titles(engine) == ["B Side", "Closing", "Middle", "Opening"]
    assert (body["releases"], body["releases_failed"]) == (2, 1)
    assert [(e["provider"], e["error_code"]) for e in body["provider_errors"]] == [("shelf", "metadata_provider_upstream_error")]
    assert "internal server error" not in str(body["provider_errors"])


def test_a_release_the_provider_cannot_find_counts_as_failed(engine, monkeypatch):
    shelf = Shelf(catalog(), unreadable=[release("Ghost", "single", [], year=2021)])
    h = harness(engine, monkeypatch, shelf)

    resp = download(h)

    assert resp.status_code == 200
    assert (resp.json()["releases"], resp.json()["releases_failed"], resp.json()["provider_errors"]) == (3, 1, [])


def test_a_provider_that_rate_limits_is_not_asked_again(engine, monkeypatch):
    shelf = Shelf(catalog(), failing={"First Album": ProviderRateLimited("slow down", retry_after=5)})
    h = harness(engine, monkeypatch, shelf)

    resp = download(h)

    # The albums come first: the first read is refused and the other two are never asked.
    assert shelf.read == ["First Album"]
    assert resp.status_code == 503 and resp.headers["Retry-After"]
    assert resp.json()["detail"]["error_code"] == "metadata_provider_rate_limited"
    assert downloads_of(engine) == [] and h.queued == []


def test_a_rate_limit_after_some_releases_keeps_what_was_read(engine, monkeypatch):
    shelf = Shelf(catalog(), failing={"Opening": ProviderRateLimited("slow down")})
    h = harness(engine, monkeypatch, shelf)

    resp = download(h)

    # First Album is read, the single is refused, the EP is not asked (albums, singles, EPs).
    assert shelf.read == ["First Album", "Opening"]
    assert resp.status_code == 200
    assert (resp.json()["releases"], resp.json()["releases_failed"]) == (1, 2)
    assert titles(engine) == ["Closing", "Middle", "Opening"]


def test_every_release_failing_is_the_providers_failure(engine, monkeypatch):
    boom = RuntimeError("connection refused")
    shelf = Shelf(catalog(), failing={"First Album": boom, "Opening": boom, "Short EP": boom})
    h = harness(engine, monkeypatch, shelf)

    resp = download(h)

    assert resp.status_code == 503
    assert resp.json()["detail"]["error_code"] == "metadata_provider_unreachable"
    assert downloads_of(engine) == []


def test_a_discography_over_the_cap_is_refused_whole(engine, monkeypatch):
    monkeypatch.setattr(discography, "MAX_RELEASES", 2)
    shelf = Shelf(catalog())
    h = harness(engine, monkeypatch, shelf)

    resp = download(h)

    assert resp.status_code == 422
    assert resp.json()["detail"] == {
        "error_code": "discography_too_large",
        "error_message": "The artist has 3 releases of the chosen types: more than the 2 one request takes.",
        "releases": 3, "max_releases": 2,
    }
    # Nothing was read nor queued; asking for fewer types goes through.
    assert shelf.read == [] and downloads_of(engine) == []
    assert download(h, release_types=["album", "ep"]).status_code == 200


def many(count: int) -> list[Album]:
    """``count`` albums of one song each, with titles that are different releases."""
    return [
        release(f"Record {chr(65 + i // 26)}{chr(65 + i % 26)}", "album", [song(f"Tune {i}", f"Record {i}")], year=1990 + i % 30)
        for i in range(count)
    ]


def set_search_limit(engine, value: int) -> None:
    with Session(engine) as session:
        SettingsRepository(session).set_system("provider.shelf.search_limit", value)
        session.commit()


def test_a_discography_beyond_one_page_and_the_search_limit_is_read_whole(engine, monkeypatch):
    monkeypatch.setattr(discography, "MAX_RELEASES", 100)
    shelf = Shelf(many(75))
    h = harness(engine, monkeypatch, shelf)
    # What the provider lists for a SEARCH has nothing to do with a discography.
    set_search_limit(engine, 10)

    resp = download(h)

    assert resp.status_code == 200, resp.text
    assert resp.json()["releases"] == 75 and len(shelf.read) == 75 and len(titles(engine)) == 75
    assert shelf.asked == [discography.LISTING_LIMIT]


def test_an_artist_above_the_cap_is_refused_with_the_honest_count(engine, monkeypatch):
    shelf = Shelf(many(discography.MAX_RELEASES + 1))
    h = harness(engine, monkeypatch, shelf)

    resp = download(h)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert (detail["error_code"], detail["releases"], detail["max_releases"]) == (
        "discography_too_large", discography.MAX_RELEASES + 1, discography.MAX_RELEASES,
    )
    assert shelf.read == [] and downloads_of(engine) == []


def test_only_the_releases_of_the_chosen_types_count_towards_the_cap(engine, monkeypatch):
    monkeypatch.setattr(discography, "MAX_RELEASES", 5)
    others = [release(f"Guest {i}", "appears_on", [song(f"Feature {i}", f"Guest {i}")]) for i in range(20)]
    shelf = Shelf([*many(5), *others])
    h = harness(engine, monkeypatch, shelf)

    assert download(h).status_code == 200
    # Asking for the others too is what makes it too large.
    assert download(h, release_types=["album", "appears_on"]).json()["detail"]["releases"] == 25


def test_a_listing_that_reaches_its_bound_is_refused_not_cut(engine, monkeypatch):
    monkeypatch.setattr(discography, "LISTING_LIMIT", 10)
    shelf = Shelf(many(12))
    h = harness(engine, monkeypatch, shelf)

    resp = download(h)

    assert resp.status_code == 422 and resp.json()["detail"]["error_code"] == "discography_too_large"
    assert shelf.read == [] and downloads_of(engine) == []


def test_the_artist_page_lists_beyond_the_search_limit_up_to_its_bound(engine, monkeypatch):
    shelf = Shelf(many(130))
    h = harness(engine, monkeypatch, shelf)
    set_search_limit(engine, 10)

    page = h.call("admin", "GET", f"/artist/shelf/{ARTIST}/albums", doc="/artist/{provider}/{id}/albums").json()

    assert shelf.asked == [RELEASES_LISTING_LIMIT]
    assert page["total"] == RELEASES_LISTING_LIMIT and page["may_be_longer"] is True


def test_the_artist_page_says_when_the_list_is_whole(engine, monkeypatch):
    h = harness(engine, monkeypatch, Shelf(many(30)))

    page = h.call("admin", "GET", f"/artist/shelf/{ARTIST}/albums", doc="/artist/{provider}/{id}/albums").json()

    assert page["total"] == 30 and page["may_be_longer"] is False


def test_the_cap_is_a_number_of_releases_not_of_editions(engine, monkeypatch):
    monkeypatch.setattr(discography, "MAX_RELEASES", 1)
    editions = [release("Album", "album", [song("One", "Album")], pid="a"),
                release("Album (Deluxe Edition)", "album", [song("One", "Album")], pid="b")]
    h = harness(engine, monkeypatch, Shelf(editions))

    assert download(h).status_code == 200


def test_no_release_of_the_chosen_types_is_refused_with_a_code(engine, monkeypatch):
    h = harness(engine, monkeypatch, Shelf([release("Greatest Hits", "compilation", [song("Hit", "Greatest Hits")])]))

    resp = download(h)

    assert resp.status_code == 422 and resp.json()["detail"]["error_code"] == "discography_empty"
    assert downloads_of(engine) == []


def test_releases_without_tracks_are_an_empty_discography(engine, monkeypatch):
    h = harness(engine, monkeypatch, Shelf([release("Hollow", "album", [])]))

    assert download(h).json()["detail"]["error_code"] == "discography_empty"


@pytest.mark.parametrize("types", [[], ["album", "remix"]])
def test_an_invalid_list_of_release_types_is_refused(engine, monkeypatch, types):
    h = harness(engine, monkeypatch, Shelf(catalog()))

    resp = h.call("admin", "POST", "/download/artist", json={"provider": "shelf", "id": ARTIST, "release_types": types})

    assert resp.status_code == 422 and downloads_of(engine) == []


# ---------------------------------------------------------------------------
# The capability
# ---------------------------------------------------------------------------


def test_a_provider_without_the_capability_is_refused_cleanly(engine, monkeypatch):
    shelf = Shelf(catalog(), supported=False)
    h = harness(engine, monkeypatch, shelf)

    resp = download(h)

    assert resp.status_code == 422
    assert resp.json()["detail"] == {
        "error_code": "discography_not_supported",
        "error_message": "Provider 'shelf' cannot list the releases of an artist with their tracks.",
    }
    assert shelf.read == [] and downloads_of(engine) == []


@pytest.mark.parametrize("supported", [True, False])
def test_the_artist_detail_says_whether_its_discography_can_be_downloaded(engine, monkeypatch, supported):
    h = harness(engine, monkeypatch, Shelf(catalog(), supported=supported))

    body = h.call("admin", "GET", f"/artist/shelf/{ARTIST}", doc="/artist/{provider}/{id}").json()

    assert body["discography_downloadable"] is supported


def test_the_capability_is_declared_not_inferred():
    # The contract's default: no capability, whatever the provider overrides.
    assert FakeMetadataProvider("plain").supports_discography() is False
    assert metadata_capabilities(as_proxy(FakeMetadataProvider("plain"))) == []
    assert metadata_capabilities(as_proxy(Shelf([]))) == [CAPABILITY_DISCOGRAPHY]
    assert metadata_capabilities(as_proxy(Shelf([], supported=False))) == []


# ---------------------------------------------------------------------------
# The approval policy
# ---------------------------------------------------------------------------


def test_a_users_discography_waits_with_the_tracks_it_needs(engine, monkeypatch):
    shelf = Shelf(catalog())
    h = harness(engine, monkeypatch, shelf)
    # "Closing" is in the library already: it needs no download.
    seed_in_library(engine, [song("Closing", "First Album", isrc="I3")])

    resp = download(h, "user")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert (body["status"], body["kind"], body["reason"], body["needed_tracks"]) == (
        "pending_approval", "artist", "kind_requires_approval", 4,
    )
    assert (body["releases"], body["releases_failed"], body["provider_errors"]) == (3, 0, [])
    (request,) = requests_of(engine)
    assert (request.kind, request.status, request.needed_tracks, request.target_provider, request.target_id) == (
        "artist", "pending", 4, "shelf", ARTIST,
    )
    # Nothing was queued while it waits.
    assert len(downloads_of(engine)) == 1 and h.queued == []


def test_an_admin_approving_the_request_queues_the_whole_discography_once(engine, monkeypatch):
    h = harness(engine, monkeypatch, Shelf(catalog()))
    request_id = download(h, "user").json()["request_id"]

    result = h.decide("admin", "approve", request_id)

    assert result["outcome"] == "decided" and result["request"]["queued_tracks"] == 5
    assert titles(engine) == ["B Side", "Closing", "Middle", "Only Here", "Opening"]
    assert len(h.queued) == 5


def test_a_users_discography_goes_through_when_the_policy_lets_it(engine, monkeypatch):
    h = harness(engine, monkeypatch, Shelf(catalog()))
    h.policy(artist_mode=dp.MODE_AUTO_UP_TO, artist_threshold=5)

    resp = download(h, "user")

    assert resp.status_code == 200, resp.text
    assert resp.json()["total_tracks"] == 5 and len(h.queued) == 5
    assert requests_of(engine)[0].status == "approved"


def test_an_admins_discography_is_queued_through_the_gate_unrecorded(engine, monkeypatch):
    h = harness(engine, monkeypatch, Shelf(catalog()))
    seed_in_library(engine, [song("Middle", "First Album", isrc="I2")])

    resp = download(h, "admin")

    assert resp.status_code == 200
    # The gate leaves out the track already in the library; nothing is recorded for an admin.
    assert (resp.json()["total_tracks"], resp.json()["skipped"]) == (4, 1)
    assert requests_of(engine) == []


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


def test_the_default_types_are_the_artists_own():
    assert OWN_RELEASE_TYPES == ("album", "single", "ep")


def test_releases_are_read_album_first_and_the_oldest_first():
    albums = [
        release("Late Single", "single", [], year=2019), release("New Album", "album", [], year=2018),
        release("Old Album", "album", [], year=2010), release("Undated EP", "ep", [], year=None),
        release("Comp", "compilation", [], year=2000), release("Plain", None, [], year=2012),
    ]

    groups = select_releases(group_releases(albums, []), OWN_RELEASE_TYPES)

    assert [g.representative.title for g in groups] == ["Old Album", "Plain", "New Album", "Late Single", "Undated EP"]


def test_a_song_is_the_same_across_releases_but_not_a_different_version_or_length():
    album = song("Opening", "First Album", duration_ms=200_000)
    assert same_song(album, song("Opening", "Opening", duration_ms=201_000))
    assert same_song(album, song("Opening!", "Other", duration_ms=0))
    assert not same_song(album, song("Opening (Live)", "Live", duration_ms=200_000))
    assert not same_song(album, song("Opening (Radio Edit)", "Single", duration_ms=200_000))
    # Two songs with one title are told apart by their length.
    assert not same_song(song("Intro", "A", duration_ms=45_000), song("Intro", "B", duration_ms=190_000))
    assert not same_song(album, make_track("Opening", "Somebody Else", "Other", duration_ms=200_000))


def test_the_first_copy_of_a_song_is_the_one_kept():
    first, second = song("Opening", "Album"), song("Opening", "Single")
    other = song("Closing", "Album")

    kept = distinct_songs([first, other, second])

    assert kept == [first, other] and kept[0] is first
