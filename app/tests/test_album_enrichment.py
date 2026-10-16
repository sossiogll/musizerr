"""
Arricchimento incrociato di un album da 16 tracce: o tutte le tracce sono
confrontate, o la risposta dice ``enrichment: {complete: false, reason}``.
Mai un arricchimento parziale in silenzio: un errore su una traccia non
ferma le successive, un provider che fallisce non ferma gli altri.
"""
from __future__ import annotations

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.interfaces import MetadataProvider
from musizerr_sdk.models import Album
from musizerr_sdk.context import StaticProviderContext
from services import enrichment
from services.enrichment import cross_enrich_album

from tests.conftest import FakeAuthManager, FakeRegistry, make_track

N = 16


def _tracks(provider: str) -> list:
    return [
        make_track(title=f"Song {i}", artist="Caparezza", album="Prisoner 709", year=2017,
                   duration_ms=200_000 + i, provider=provider, provider_id=f"{provider}-{i}")
        for i in range(N)
    ]


def _album(provider: str) -> Album:
    return Album(title="Prisoner 709", artist="Caparezza", year=2017, track_count=N,
                 provider=provider, provider_id=f"{provider}-album", tracks=_tracks(provider))


class _Other(MetadataProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name, *, fail_search=False, fail_album=False):
        super().__init__(StaticProviderContext())
        self.name = name
        self.label = name
        self._fail_search = fail_search
        self._fail_album = fail_album

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def search_albums(self, query, session, limit=20):
        if self._fail_search:
            raise RuntimeError("503 rate limited")
        return [_album(self.name)]

    def get_album(self, provider_id, session):
        if self._fail_album:
            raise RuntimeError("timeout")
        return _album(self.name)


def test_all_sixteen_tracks_are_enriched_and_the_outcome_is_complete():
    album = _album("main")

    outcome = cross_enrich_album(album, FakeRegistry(metadata=[_Other("other")]), FakeAuthManager())

    assert all("other" in t.providers for t in album.tracks)
    assert outcome.as_dict() == {"complete": True, "reason": None}


def test_a_failure_on_one_track_does_not_stop_the_others(monkeypatch):
    album = _album("main")
    real_enrich = enrichment.enrich

    def flaky(existing, candidate):
        if getattr(existing, "title", "") == "Song 4":
            raise ValueError("bad data")
        return real_enrich(existing, candidate)

    monkeypatch.setattr(enrichment, "enrich", flaky)

    outcome = cross_enrich_album(album, FakeRegistry(metadata=[_Other("other")]), FakeAuthManager())

    enriched = [t.title for t in album.tracks if "other" in t.providers]
    assert len(enriched) == N - 1 and "Song 4" not in enriched
    assert outcome.as_dict() == {"complete": False, "reason": "track_error"}


def test_a_failing_provider_is_reported_and_the_next_one_still_runs():
    album = _album("main")
    registry = FakeRegistry(metadata=[_Other("broken", fail_search=True), _Other("other")])

    outcome = cross_enrich_album(album, registry, FakeAuthManager())

    assert all("other" in t.providers for t in album.tracks)
    assert outcome.as_dict() == {"complete": False, "reason": "provider_error"}


def test_a_failing_album_fetch_is_reported():
    album = _album("main")

    outcome = cross_enrich_album(album, FakeRegistry(metadata=[_Other("other", fail_album=True)]), FakeAuthManager())

    assert outcome.complete is False and outcome.reason == "provider_error"


def test_no_matching_album_elsewhere_is_still_complete():
    album = Album(title="Unknown", artist="Nobody", year=1999, provider="main", provider_id="x", tracks=_tracks("main"))

    outcome = cross_enrich_album(album, FakeRegistry(metadata=[_Other("other")]), FakeAuthManager())

    assert outcome.complete is True


# ---------------------------------------------------------------------------
# A provider whose search lists albums without their year
# ---------------------------------------------------------------------------

REMIXES = [
    "E si fa bello per te - Remastered 2023",
    "E si fa bello per te - Federico Scavo Remix",
    "E si fa bello per te - Soul Speech Remix",
    "E si fa bello per te - Rivaz & Botteghi Remix",
]


def _remix_album(provider: str, *, year, previews: bool) -> Album:
    tracks = []
    for index, title in enumerate(REMIXES):
        track = make_track(title, "Jovanotti", "E si fa bello per te (i remix)", year=2023, duration_ms=190_000 + index * 1000,
                           provider=provider, provider_id=f"{provider}-{index}")
        track.track_number = index + 1
        track.preview_url = f"https://cdn.example.com/{provider}-{index}.mp3" if previews else None
        tracks.append(track)
    return Album(title="E si fa bello per te (i remix)", artist="Jovanotti", year=year, track_count=len(tracks),
                 album_type="single", provider=provider, provider_id=f"{provider}-album", tracks=tracks)


class _YearOnlyInTheDetail(MetadataProvider):
    """A search that lists albums without their release date (the detail has it)."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name="listed", *, detail_year=2023, fail_detail=False):
        super().__init__(StaticProviderContext())
        self.name = name
        self.label = name
        self.detail_year, self.fail_detail = detail_year, fail_detail
        self.detail_calls = 0

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def search_albums(self, query, session, limit=20):
        return [_remix_album(self.name, year=None, previews=True)]

    def get_album(self, provider_id, session):
        self.detail_calls += 1
        if self.fail_detail:
            raise RuntimeError("timeout")
        return _remix_album(self.name, year=self.detail_year, previews=True)


def test_an_album_is_enriched_from_a_provider_that_lists_it_without_a_year():
    """Seen live: the album of 10 remixes of one catalog stayed with ``providers: []`` and
    no previews, while the edition of another catalog has previews: its search omits the
    year, and ``match_album`` asks for the year of both sides."""
    album = _remix_album("main", year=2023, previews=False)
    listed = _YearOnlyInTheDetail()

    outcome = cross_enrich_album(album, FakeRegistry(metadata=[listed]), FakeAuthManager())

    assert "listed" in album.providers
    assert all(t.preview_url for t in album.tracks), "every remix pairs with its own version and brings its preview"
    assert [t.preview_url.rsplit("/", 1)[-1] for t in album.tracks] == [f"listed-{i}.mp3" for i in range(len(REMIXES))]
    assert listed.detail_calls == 1, "the detail serves the year and the tracks: asked once"
    assert outcome.as_dict() == {"complete": True, "reason": None}


def test_the_year_of_the_detail_still_has_to_agree():
    album = _remix_album("main", year=2023, previews=False)

    cross_enrich_album(album, FakeRegistry(metadata=[_YearOnlyInTheDetail(detail_year=1999)]), FakeAuthManager())

    assert album.providers == [] and not any(t.preview_url for t in album.tracks)


def test_a_candidate_that_is_another_album_costs_no_detail_call():
    album = _remix_album("main", year=2023, previews=False)
    album.title = "Another record"
    listed = _YearOnlyInTheDetail()

    cross_enrich_album(album, FakeRegistry(metadata=[listed]), FakeAuthManager())

    assert listed.detail_calls == 0


def test_a_detail_that_fails_is_reported_not_silent():
    album = _remix_album("main", year=2023, previews=False)

    outcome = cross_enrich_album(album, FakeRegistry(metadata=[_YearOnlyInTheDetail(fail_detail=True)]), FakeAuthManager())

    assert outcome.as_dict() == {"complete": False, "reason": "provider_error"}
    assert album.providers == []
