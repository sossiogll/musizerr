"""
The cross-enrichments ask only the providers whose manifest says they can
answer (``proxy.implements(...)``, ``supports_isrc_lookup()``), BEFORE
authenticating them: a provider that has no search (a likes-only one) is
neither signed in nor reported, so a guest's enrichment stays complete instead
of ``provider_error`` for a question nobody could answer.
"""
from __future__ import annotations

from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizeer_sdk.context import StaticProviderContext
from musizeer_sdk.identifiers import Identifier
from musizeer_sdk.interfaces import MetadataProvider
from musizeer_sdk.models import Album, Artist
from services.enrichment import (
    complete_universal_ids,
    cross_enrich_album,
    cross_enrich_artist,
    cross_enrich_artist_albums,
    cross_enrich_track,
)

from tests.conftest import FakeAuthManager, FakeMetadataProvider, FakeRegistry, make_track


class _SignInRefused(Exception):
    pass


class _GuestAuthManager(FakeAuthManager):
    """Refuses to sign in the providers named in ``refuses`` (a guest on a
    provider that needs an account) and records who was authenticated."""

    def __init__(self, *refuses: str) -> None:
        super().__init__()
        self.refuses = set(refuses)
        self.authenticated: list[str] = []

    def authenticate(self, provider):
        self.authenticated.append(provider.name)
        if provider.name in self.refuses:
            raise _SignInRefused(provider.name)
        return super().authenticate(provider)


class _LikesOnly(MetadataProvider):
    """Only the user's likes: no search, no detail, no lookup."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name="likes") -> None:
        super().__init__(StaticProviderContext())
        self.name = name
        self.label = name

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def supports_user_library(self) -> bool:
        return True

    def get_saved_tracks(self, session, limit=50, offset=0):
        return []


class _IsrcOnly(_LikesOnly):
    """Answers an ISRC lookup and nothing else."""

    def __init__(self, name="isrc-only") -> None:
        super().__init__(name)
        self.lookups = 0

    def supports_isrc_lookup(self) -> bool:
        return True

    def find_by_isrc(self, isrc, session):
        self.lookups += 1
        return make_track("Song", "Artist", "Album", provider=self.name, provider_id=f"{self.name}-1", isrc=isrc)


class _DetailOnly(_LikesOnly):
    def __init__(self, name="detail-only") -> None:
        super().__init__(name)
        self.detail_calls = 0

    def get_track(self, provider_id, session):
        self.detail_calls += 1
        return make_track("Song", "Artist", "Album", provider=self.name, provider_id=provider_id,
                          isrc="ISRC0000001")


def _album(provider: str = "main") -> Album:
    tracks = [make_track(f"Song {i}", "Artist", "Record", year=2020, duration_ms=200_000 + i, provider=provider,
                         provider_id=f"{provider}-{i}") for i in range(3)]
    return Album(title="Record", artist="Artist", year=2020, track_count=3, provider=provider,
                 provider_id=f"{provider}-album", tracks=tracks)


# --- album ---------------------------------------------------------------

def test_album_a_provider_without_search_albums_is_neither_signed_in_nor_reported():
    auth = _GuestAuthManager("likes")
    searchable = FakeMetadataProvider("catalog", albums=[_album("catalog")])
    registry = FakeRegistry(metadata=[_LikesOnly("likes"), searchable])

    outcome = cross_enrich_album(_album(), registry, auth)

    assert "likes" not in auth.authenticated
    assert "catalog" in auth.authenticated
    assert outcome.as_dict() == {"complete": True, "reason": None}


def test_album_a_provider_that_implements_search_albums_and_fails_is_still_reported():
    auth = _GuestAuthManager("catalog")
    registry = FakeRegistry(metadata=[FakeMetadataProvider("catalog", albums=[_album("catalog")])])

    outcome = cross_enrich_album(_album(), registry, auth)

    assert outcome.as_dict() == {"complete": False, "reason": "provider_error"}


# --- track ---------------------------------------------------------------

def test_track_a_provider_that_cannot_find_tracks_is_not_signed_in():
    auth = _GuestAuthManager("likes")
    track = make_track("Song", "Artist", "Album", provider="main")
    found = make_track("Song", "Artist", "Album", provider="catalog", provider_id="c1")
    registry = FakeRegistry(metadata=[_LikesOnly("likes"), FakeMetadataProvider("catalog", tracks=[found])])

    cross_enrich_track(track, registry, auth)

    assert auth.authenticated == ["catalog"]
    assert "catalog" in track.providers


def test_track_an_isrc_lookup_alone_is_enough_when_the_track_has_an_isrc():
    track = make_track("Song", "Artist", "Album", provider="main", isrc="ISRC0000001")
    lookup = _IsrcOnly()
    auth = _GuestAuthManager()

    cross_enrich_track(track, FakeRegistry(metadata=[lookup]), auth)

    assert auth.authenticated == ["isrc-only"] and lookup.lookups == 1


def test_track_an_isrc_lookup_alone_is_not_asked_about_a_track_without_isrc():
    track = make_track("Song", "Artist", "Album", provider="main")
    lookup = _IsrcOnly()
    auth = _GuestAuthManager()

    cross_enrich_track(track, FakeRegistry(metadata=[lookup]), auth)

    assert auth.authenticated == [] and lookup.lookups == 0


def test_track_the_provider_of_the_track_is_asked_for_variants_only_when_it_implements_them():
    track = make_track("Song", "Artist", "Album", provider="likes")
    auth = _GuestAuthManager()

    cross_enrich_track(track, FakeRegistry(metadata=[_LikesOnly("likes")]), auth)

    assert auth.authenticated == []


# --- universal ids -------------------------------------------------------

def test_universal_ids_are_asked_only_of_a_provider_that_has_the_detail():
    auth = _GuestAuthManager("likes")
    row = make_track("Song", "Artist", "Album", provider="likes", provider_id="l1")

    complete_universal_ids([row], FakeRegistry(metadata=[_LikesOnly("likes")]), auth)

    assert auth.authenticated == []
    detail = _DetailOnly()
    row = make_track("Song", "Artist", "Album", provider="detail-only", provider_id="d1")

    complete_universal_ids([row], FakeRegistry(metadata=[detail]), auth)

    assert detail.detail_calls == 1 and row.get_id("isrc") == Identifier(type="isrc", value="ISRC0000001")


# --- artist ----------------------------------------------------------------

def test_artist_a_provider_without_search_artists_is_not_signed_in():
    auth = _GuestAuthManager("likes")
    artist = Artist(name="Artist", provider="main", provider_id="a0")
    found = Artist(name="Artist", provider="catalog", provider_id="a1")
    registry = FakeRegistry(metadata=[_LikesOnly("likes"), FakeMetadataProvider("catalog", artists=[found])])

    cross_enrich_artist(artist, registry, auth)

    assert auth.authenticated == ["catalog"]
    assert "catalog" in artist.providers


def test_artist_albums_a_provider_that_cannot_list_a_discography_is_not_signed_in_nor_reported():
    auth = _GuestAuthManager("likes")
    failures = _Failures()
    registry = FakeRegistry(metadata=[_LikesOnly("likes")])

    result = cross_enrich_artist_albums("Artist", "main", [_album()], registry, auth, failures=failures)

    assert auth.authenticated == [] and failures.recorded == []
    assert len(result) == 1


class _Failures:
    def __init__(self) -> None:
        self.recorded: list[str] = []

    def record(self, name, exc):
        self.recorded.append(name)
