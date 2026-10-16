"""
Un provider che fallisce non è un errore dell'host.

- Le rotte di dettaglio rispondono 502 (il provider ha risposto con un
  errore) o 503 riprovabile (irraggiungibile, lento, non configurato), con
  ``error_code`` e il testo grezzo solo per un amministratore; 404 se il
  provider dice che la risorsa non esiste. Mai 500.
- Le rotte che aggregano più provider restituiscono ciò che è riuscito e
  nominano in ``provider_errors`` chi ha fallito; un provider non
  configurato non è un fallimento.

Le risposte sono validate contro ``app/openapi.yaml``.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

import main
from musizerr_sdk.auth import AccountModel, ApiKeyConfig, AuthDefinition, AuthFlow, AuthSession
from core.auth_manager import ProviderNotConfigured
from musizerr_sdk.interfaces import LyricsProvider, MetadataProvider
from musizerr_sdk.models import Lyrics
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.errors import ProviderRateLimited
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import User
from routes import deps
from services.config_manager import ConfigManager
from services.errors import classify_error, http_status_for

from tests.conftest import FakeRegistry
from tests.database import new_engine
from tests.test_openapi_responses import _Catalog, assert_conforms

UPSTREAM_URL = "https://upstream.example/v1/albums/x?token=secret"


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", UPSTREAM_URL)
    return httpx.HTTPStatusError(
        f"Server error '{code}' for url '{UPSTREAM_URL}'", request=request,
        response=httpx.Response(code, request=request),
    )


ERRORS = {
    "bad_gateway": _status_error(502),
    "gone": _status_error(404),
    "refused": httpx.ConnectError("[Errno 111] Connection refused"),
    "slow": httpx.ReadTimeout("timed out"),
    "limited": _status_error(429),
    "denied": _status_error(401),
    "quota": ProviderRateLimited("upstream quota or rate limit reached (quotaExceeded)"),
    "parse": ValueError("unexpected payload"),
}


class _Broken(MetadataProvider):
    """Ogni lettura solleva ``error``: il provider remoto è guasto."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name: str, error: BaseException):
        super().__init__(StaticProviderContext())
        self.name, self.label, self._error = name, name, error

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def _fail(self, *args, **kwargs):
        raise self._error

    get_track = get_album = get_artist = get_playlist = get_artist_albums = _fail
    get_artist_top_tracks = find_related_tracks = find_similar_tracks = find_by_isrc = _fail

    def supports_isrc_lookup(self):
        return True

    search_tracks = search_albums = search_artists = search_playlists = _fail
    get_new_releases = get_trending_albums = get_trending_tracks = get_trending_artists = _fail


class _Unset(_Broken):
    """Un provider a chiave API senza chiave: l'autenticazione (vera, per la
    ricerca, o finta) lo dice."""

    auth = AuthDefinition(flow=AuthFlow.API_KEY, account_model=AccountModel.SYSTEM, api_key=ApiKeyConfig(query_param="key"))

    def __init__(self):
        super().__init__("unset", RuntimeError("never called"))


class _Auth:
    def authenticate(self, provider):
        if provider.name == "unset":
            raise ProviderNotConfigured("No API key configured for provider 'unset'")
        return AuthSession.anonymous()


class _Lyrics(LyricsProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name, *, error=None, text=None):
        super().__init__(StaticProviderContext())
        self.name, self.label, self._error, self._text = name, name, error, text

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_lyrics(self, track, session):
        if self._error:
            raise self._error
        return Lyrics(plain=self._text, source=self.name) if self._text else None


@pytest.fixture
def make_api():
    installed = []

    def _make(metadata, *, lyrics=(), syncs=(), auth_enabled=True, settings=None):
        engine = new_engine()
        with Session(engine) as session:
            users = {role: User(username=role, password_hash="x", role=role) for role in ("user", "admin")}
            session.add_all(users.values())
            session.commit()
            tokens = {role: create_access_token(u.id, u.role) for role, u in users.items()}
        config = ConfigManager(engine=engine)
        config.update({"auth_enabled": auth_enabled, "metadata_provider_priority": [p.name for p in metadata], **(settings or {})})
        registry = FakeRegistry(metadata=list(metadata), lyrics=list(lyrics), syncs=list(syncs))

        def _session():
            with Session(engine) as s:
                yield s

        overrides = {
            get_session: _session,
            deps.get_config: lambda: config,
            deps.get_registry: lambda: registry,
            deps.get_auth_manager: lambda: _Auth(),
        }
        main.app.dependency_overrides.update(overrides)
        installed.extend(overrides)
        from routes import discover
        with discover._cache_lock:
            discover._cache.clear()
        client = TestClient(main.app, raise_server_exceptions=False)

        def get(url, role="guest"):
            headers = {"Authorization": f"Bearer {tokens[role]}"} if role in tokens else {}
            return client.get(url, headers=headers)

        return get

    yield _make
    for key in installed:
        main.app.dependency_overrides.pop(key, None)


# ---------------------------------------------------------------------------
# Classificazione
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, kind, status", [
    ("bad_gateway", "upstream_error", 502),
    ("gone", "not_found", 404),
    ("refused", "unreachable", 503),
    ("slow", "timeout", 503),
    ("limited", "rate_limited", 503),
    ("quota", "rate_limited", 503),
    ("denied", "auth_failed", 502),
    ("parse", "failed", 502),
])
def test_errors_are_classified_by_type_then_status(name, kind, status):
    assert classify_error(ERRORS[name]) == kind
    assert http_status_for(kind) == status


def test_a_missing_credential_is_not_a_fault():
    assert classify_error(ProviderNotConfigured("x")) == "not_configured"
    assert http_status_for("not_configured") == 503


# ---------------------------------------------------------------------------
# Rotte di dettaglio
# ---------------------------------------------------------------------------

_DETAIL = [
    ("/album/broken/x", "/album/{provider}/{id}"),
    ("/playlist/broken/x", "/playlist/{provider}/{id}"),
    ("/track/broken/x", "/track/{provider}/{id}"),
    ("/track/broken/x/lyrics", "/track/{provider}/{id}/lyrics"),
    ("/track/broken/x/related", "/track/{provider}/{id}/related"),
    ("/track/broken/x/appears-on", "/track/{provider}/{id}/appears-on"),
    ("/artist/broken/x", "/artist/{provider}/{id}"),
    ("/artist/broken/x/albums", "/artist/{provider}/{id}/albums"),
]


@pytest.mark.parametrize("url, path", _DETAIL)
def test_an_upstream_error_is_a_502_never_a_500(make_api, url, path):
    get = make_api([_Broken("broken", ERRORS["bad_gateway"])])

    resp = get(url)

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "metadata_provider_upstream_error"
    assert detail["provider"] == "broken"
    assert "error_detail" not in detail and "secret" not in resp.text
    assert_conforms(resp, "GET", path)


@pytest.mark.parametrize("url, path", _DETAIL)
def test_an_unreachable_provider_is_a_retryable_503(make_api, url, path):
    get = make_api([_Broken("broken", ERRORS["refused"])])

    resp = get(url, "user")

    assert resp.status_code == 503, resp.text
    assert resp.headers["retry-after"].isdigit()
    assert resp.json()["detail"]["error_code"] == "metadata_provider_unreachable"
    assert_conforms(resp, "GET", path)


@pytest.mark.parametrize("retry_after, expected", [
    (None, "30"),       # the provider does not say: the default
    (346.2, "347"),     # the provider says: believed, rounded up
    (0, "30"),
    (86400, "3600"),    # an exhausted daily quota: capped
])
def test_a_rate_limit_says_how_long_to_wait(make_api, retry_after, expected):
    get = make_api([_Broken("broken", ProviderRateLimited("limited", retry_after=retry_after))])

    resp = get("/album/broken/x", "user")

    assert resp.status_code == 503, resp.text
    assert resp.headers["retry-after"] == expected
    assert resp.json()["detail"]["error_code"] == "metadata_provider_rate_limited"
    assert_conforms(resp, "GET", "/album/{provider}/{id}")


def test_a_rate_limit_without_a_wait_still_constructs_like_before():
    error = ProviderRateLimited("quota")

    assert error.retry_after is None and str(error) == "quota"


def test_the_raw_error_is_for_admins_only(make_api):
    get = make_api([_Broken("broken", ERRORS["bad_gateway"])])

    assert "error_detail" not in get("/album/broken/x", "user").json()["detail"]
    admin = get("/album/broken/x", "admin")
    assert admin.json()["detail"]["error_detail"].startswith("Server error '502'")
    assert_conforms(admin, "GET", "/album/{provider}/{id}")


def test_with_auth_off_everyone_is_the_admin(make_api):
    get = make_api([_Broken("broken", ERRORS["bad_gateway"])], auth_enabled=False)

    assert "error_detail" in get("/album/broken/x").json()["detail"]


def test_the_provider_saying_not_found_is_a_404(make_api):
    get = make_api([_Broken("broken", ERRORS["gone"])])

    resp = get("/album/broken/x")

    assert resp.status_code == 404
    assert_conforms(resp, "GET", "/album/{provider}/{id}")


def test_an_unconfigured_provider_is_a_503(make_api):
    get = make_api([_Unset()])

    resp = get("/album/unset/x")

    assert resp.status_code == 503
    assert resp.json()["detail"]["error_code"] == "metadata_provider_not_configured"


def test_a_failing_lyrics_provider_does_not_hide_the_next_one(make_api):
    get = make_api([_Catalog()], lyrics=[_Lyrics("la", error=ERRORS["refused"]), _Lyrics("lb", text="la la")])

    resp = get("/track/catalog/t1/lyrics")

    assert resp.status_code == 200 and resp.json()["plain"] == "la la"


def test_lyrics_not_found_only_when_nobody_failed(make_api):
    failing = make_api([_Catalog()], lyrics=[_Lyrics("la", error=ERRORS["refused"]), _Lyrics("lb")])
    assert failing("/track/catalog/t1/lyrics").status_code == 503


def test_lyrics_missing_everywhere_is_a_404(make_api):
    get = make_api([_Catalog()], lyrics=[_Lyrics("lb")])
    assert get("/track/catalog/t1/lyrics").status_code == 404


# ---------------------------------------------------------------------------
# Rotte aggregate
# ---------------------------------------------------------------------------

_AGGREGATED = [
    ("/search?q=song&type=track", "/search", "user"),
    ("/search?q=song&type=album", "/search", "user"),
    ("/search?q=song&type=artist", "/search", "user"),
    ("/search?q=song&type=playlist", "/search", "user"),
    ("/discover/new-releases", "/discover/new-releases", "guest"),
    ("/discover/trending/albums", "/discover/trending/albums", "guest"),
    ("/discover/trending/tracks", "/discover/trending/tracks", "guest"),
    ("/discover/trending/artists", "/discover/trending/artists", "guest"),
    ("/track/catalog/t1/related", "/track/{provider}/{id}/related", "guest"),
    ("/track/catalog/t1/appears-on", "/track/{provider}/{id}/appears-on", "guest"),
    ("/artist/catalog/ar1/albums", "/artist/{provider}/{id}/albums", "guest"),
]


class _CatalogWithCharts(_Catalog):
    def get_new_releases(self, session, limit=20):
        return [self.get_album("a1", session)]

    def get_trending_albums(self, session, limit=20):
        return [self.get_album("a1", session)]

    def get_trending_tracks(self, session, limit=20):
        return [self._track()]

    def get_trending_artists(self, session, limit=20):
        return [self.get_artist("ar1", session)]


@pytest.mark.parametrize("url, path, role", _AGGREGATED)
def test_aggregated_routes_return_what_succeeded_and_name_who_failed(make_api, url, path, role):
    get = make_api([_CatalogWithCharts(), _Broken("broken", ERRORS["bad_gateway"]), _Unset()])

    resp = get(url, role)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"], "the healthy provider's results are kept"
    assert body["provider_errors"] == [{
        "provider": "broken",
        "error_code": "metadata_provider_upstream_error",
        "error_message": "The provider answered with an error.",
    }]
    assert_conforms(resp, "GET", path)


@pytest.mark.parametrize("url, path, role", _AGGREGATED)
def test_nothing_failed_means_an_empty_list(make_api, url, path, role):
    get = make_api([_CatalogWithCharts(), _Unset()])

    resp = get(url, role)

    assert resp.status_code == 200 and resp.json()["provider_errors"] == []


def test_top_tracks_name_the_failing_provider(make_api):
    get = make_api([_Catalog(), _Broken("broken", ERRORS["refused"])])

    body = get("/artist/catalog/ar1/top-tracks").json()

    assert body["items"] and body["error_code"] is None
    # Il provider guasto non ha un id per l'artista: non viene interrogato.
    assert body["provider_errors"] == []


def test_a_slow_provider_is_reported_as_a_timeout(make_api, monkeypatch):
    import threading

    release = threading.Event()

    class _Slow(_Broken):
        def search_tracks(self, query, session, limit=20):
            release.wait(5)
            return []

    monkeypatch.setattr(ConfigManager, "search_provider_budget_seconds", 0.2, raising=False)
    get = make_api([_Catalog(), _Slow("slow", RuntimeError())])
    try:
        body = get("/search?q=song", "user").json()
    finally:
        release.set()

    assert body["items"]
    assert body["provider_errors"][0]["provider"] == "slow"
    assert body["provider_errors"][0]["error_code"] == "metadata_provider_timeout"


def test_an_incomplete_discover_result_is_cached_briefly(make_api, monkeypatch):
    from routes import discover

    get = make_api([_CatalogWithCharts(), _Broken("broken", ERRORS["bad_gateway"])])
    get("/discover/new-releases")

    with discover._cache_lock:
        expires_at = discover._cache["new-releases"][0]
    import time
    assert expires_at - time.time() <= discover._INCOMPLETE_CACHE_TTL
