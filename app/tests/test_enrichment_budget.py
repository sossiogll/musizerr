"""
L'arricchimento di una risposta (gli altri provider di metadati, la
disponibilità nel sync provider) non la trattiene oltre il suo budget:

- a scadenza la risposta parte con ciò che ha, e chi non ha risposto è in
  ``provider_errors`` come timeout;
- un provider che fallisce in una richiesta non viene più interrogato in
  quella richiesta (interruttore per richiesta);
- lo stesso vale per la ricerca e per le pagine di traccia, album e artista.

I provider finti restano appesi finché il test non li libera.
"""
from __future__ import annotations

import threading
import time

import httpx
import pytest

from core.provider_failures import ProviderFailures
from services.enrichment import enrich_tracks_with_stream_url
from services.request_budget import ProviderSkipped, RequestBudget

from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider, make_track
from tests.test_openapi_responses import _Catalog, assert_conforms
from tests.test_provider_failures import _Broken, make_api  # noqa: F401 — make_api is a fixture

BUDGET = 0.5
# Tempo concesso oltre il budget: la lettura principale e il TestClient.
MARGIN = 1.5


@pytest.fixture(autouse=True)
def _no_cached_availability():
    """Il sync provider va davvero interrogato: niente esiti di altri test."""
    from services import enrichment
    enrichment._avail_cache.clear()
    yield
    enrichment._avail_cache.clear()


class _Unstreamed(_Catalog):
    """Il catalogo dei test OpenAPI, con tracce che non si possono ancora
    ascoltare: la loro disponibilità si chiede al sync provider."""

    def _track(self):
        track = super()._track()
        track.stream_url = None
        return track


@pytest.fixture
def release():
    """Libera i provider appesi a fine test, così i loro thread finiscono."""
    event = threading.Event()
    yield event
    event.set()


class _Hanging(_Broken):
    """Un provider di metadati che non risponde mai (fino a fine test)."""

    def __init__(self, name: str, release: threading.Event):
        super().__init__(name, RuntimeError("never raised"))
        self._release = release
        self.calls = 0

    def _hang(self, *args, **kwargs):
        self.calls += 1
        self._release.wait(30)
        return []

    search_tracks = search_albums = search_artists = find_related_tracks = find_similar_tracks = _hang
    get_album = get_artist_albums = _hang

    def supports_isrc_lookup(self):
        return True

    def find_by_isrc(self, isrc, session):
        self._hang()
        return None


class _HangingSync(FakeSyncProvider):
    def __init__(self, release: threading.Event):
        super().__init__("slowsync")
        self._release = release

    def find_candidates(self, track, session):
        self.availability_calls += 1
        self._release.wait(30)
        return []


class _DownSync(FakeSyncProvider):
    def __init__(self):
        super().__init__("downsync")

    def find_candidates(self, track, session):
        self.availability_calls += 1
        raise httpx.ConnectError("[Errno 113] No route to host")


def _timed(get, url, role="user"):
    started = time.monotonic()
    resp = get(url, role)
    return resp, time.monotonic() - started


def _errors(body) -> dict[str, str]:
    return {e["provider"]: e["error_code"] for e in body["provider_errors"]}


# ---------------------------------------------------------------------------
# RequestBudget
# ---------------------------------------------------------------------------

def test_a_call_that_hangs_ends_at_the_deadline_and_trips_the_provider(release):
    budget = RequestBudget(0.2)
    started = time.monotonic()

    with pytest.raises(ProviderSkipped):
        budget.call("slow", release.wait, 30)

    assert time.monotonic() - started < 0.2 + 0.3
    assert [f.provider for f in budget.failures] == ["slow"]
    assert not budget.usable("slow")


def test_a_failing_provider_is_not_asked_again_in_the_same_request():
    budget = RequestBudget(5)
    calls = []

    def boom():
        calls.append(1)
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        budget.call("down", boom)
    with pytest.raises(ProviderSkipped):
        budget.call("down", boom)

    assert len(calls) == 1
    assert budget.call("other", lambda: "ok") == "ok"


def test_not_found_is_not_a_fault():
    budget = RequestBudget(5)
    request = httpx.Request("GET", "https://x")
    missing = httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))

    def gone():
        raise missing

    with pytest.raises(httpx.HTTPStatusError):
        budget.call("p", gone)
    assert budget.usable("p") and len(budget.failures) == 0


def test_with_no_time_left_a_provider_is_skipped_and_reported():
    failures = ProviderFailures()
    budget = RequestBudget(0, failures)

    assert budget.skips("late", "sync_provider")
    assert [(f.provider, f.subject) for f in failures] == [("late", "sync_provider")]


def test_stream_url_checks_stop_at_the_first_sync_failure(monkeypatch):
    from services.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "search_stream_enrich_workers", 1, raising=False)
    sync = _DownSync()
    budget = RequestBudget(5)
    tracks = [make_track(f"Song {i}", "Artist") for i in range(20)]

    enrich_tracks_with_stream_url(tracks, FakeRegistry(syncs=[sync]), FakeAuthManager(), budget)

    assert sync.availability_calls == 1
    assert [(f.provider, f.subject) for f in budget.failures] == [("downsync", "sync_provider")]
    assert all(t.stream_url is None for t in tracks)


# ---------------------------------------------------------------------------
# Rotte
# ---------------------------------------------------------------------------

SETTINGS = {"enrichment_budget_seconds": BUDGET}


def test_search_answers_within_budget_when_the_sync_provider_hangs(make_api, release):
    get = make_api([_Unstreamed()], syncs=[_HangingSync(release)], settings=SETTINGS)

    resp, elapsed = _timed(get, "/search?q=song&type=track")

    assert resp.status_code == 200, resp.text
    assert elapsed < BUDGET + MARGIN
    body = resp.json()
    assert body["items"], "the search results are kept"
    assert _errors(body) == {"slowsync": "sync_provider_timeout"}
    assert_conforms(resp, "GET", "/search")


def test_search_reports_an_unreachable_sync_provider(make_api):
    sync = _DownSync()
    get = make_api([_Unstreamed()], syncs=[sync], settings=SETTINGS)

    body = get("/search?q=song&type=album", "user").json()

    assert body["items"]
    assert _errors(body) == {"downsync": "sync_provider_unreachable"}


@pytest.mark.parametrize("url, path, streams", [
    ("/track/catalog/t1", "/track/{provider}/{id}", True),
    ("/album/catalog/a1", "/album/{provider}/{id}", True),
    ("/artist/catalog/ar1", "/artist/{provider}/{id}", False),
    ("/artist/catalog/ar1/albums", "/artist/{provider}/{id}/albums", False),
    ("/track/catalog/t1/related", "/track/{provider}/{id}/related", False),
    ("/track/catalog/t1/appears-on", "/track/{provider}/{id}/appears-on", False),
])
def test_detail_pages_answer_within_budget_when_another_provider_hangs(make_api, release, url, path, streams):
    hanging = _Hanging("hanging", release)
    get = make_api([_Unstreamed(), hanging], syncs=[_HangingSync(release)], settings=SETTINGS)

    resp, elapsed = _timed(get, url)

    assert resp.status_code == 200, resp.text
    assert elapsed < BUDGET + MARGIN
    body = resp.json()
    assert _errors(body)["hanging"] == "metadata_provider_timeout"
    if streams:
        # The time ran out before the sync provider could be asked: skipped, and said so.
        assert _errors(body)["slowsync"] == "sync_provider_timeout"
    # Asked at most once: after its timeout it is skipped for the rest of the request.
    assert hanging.calls == 1
    assert_conforms(resp, "GET", path)


def test_the_album_says_its_enrichment_is_incomplete(make_api, release):
    get = make_api([_Unstreamed(), _Hanging("hanging", release)], settings=SETTINGS)

    body = get("/album/catalog/a1", "user").json()

    assert body["enrichment"] == {"complete": False, "reason": "provider_error"}
    assert body["tracks"], "the album itself is complete"


def test_nothing_failing_means_no_provider_errors_on_a_detail_page(make_api):
    get = make_api([_Unstreamed()], syncs=[FakeSyncProvider(available={"song": "s1"})], settings=SETTINGS)

    body = get("/track/catalog/t1", "user").json()

    assert body["provider_errors"] == []
    assert body["stream_url"]


def test_workers_are_not_bounded():
    """Senza budget (i worker) l'arricchimento resta quello di sempre."""
    sync = FakeSyncProvider(available={"song": "s1"})
    track = make_track("Song", "Artist")

    enrich_tracks_with_stream_url([track], FakeRegistry(syncs=[sync]), FakeAuthManager())

    assert track.stream_url
