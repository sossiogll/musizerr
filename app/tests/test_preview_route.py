"""
``GET /preview/{provider}/{id}``: previews whose URL is signed for a few
minutes are served through the host, which asks the provider for a fresh URL
when the listen starts (``MetadataProvider.get_preview_url``).

What broke: ``/discover`` cached the trending tracks for an hour with
preview URLs signed for 15 minutes, and the browser kept its copy for a week;
an expired URL answers 403 and the player stayed silent.
"""
from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

import main
from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.models import Track
from musizerr_sdk.context import StaticProviderContext
from db.engine import get_session
from routes import deps, discover, stream
from services.config_manager import ConfigManager

from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeMetadataProvider, FakeRegistry

AUDIO = b"ID3" + bytes(range(256)) * 8
SIGNATURE_S = 900


class _SignedCdn:
    """An origin whose URLs carry an expiry, like a CDN's signed links."""

    def __init__(self) -> None:
        self.now = time.time()
        self.requests: list[str] = []

    def url(self, item_id: str) -> str:
        return f"https://cdn.test/{item_id}.mp3?exp={int(self.now + SIGNATURE_S)}"

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if int(request.url.params["exp"]) < self.now:
            return httpx.Response(403, text="expired")
        return httpx.Response(200, content=AUDIO, headers={"Content-Type": "audio/mpeg"})


class _SignedPreviews(FakeMetadataProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, cdn: _SignedCdn, name: str = "signed") -> None:
        super().__init__(name)
        self.context = StaticProviderContext(provider_name=name)
        self.cdn = cdn
        self.resolved = 0

    def get_trending_tracks(self, session, limit=20):
        return [Track(title="Song", artist="A", album="B", duration_ms=30_000, provider=self.name, provider_id="1",
                      preview_url=self.context.preview_url("1"))]

    def get_preview_url(self, provider_id, session):
        self.resolved += 1
        return self.cdn.url(provider_id) if provider_id != "none" else None


class _Loop(_SignedPreviews):
    def get_trending_tracks(self, session, limit=20):
        return []

    def get_preview_url(self, provider_id, session):
        return self.context.preview_url(provider_id)


@pytest.fixture
def cdn(monkeypatch):
    origin = _SignedCdn()
    monkeypatch.setattr(stream, "_upstream_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(origin.handle)))
    stream.clear_preview_urls()
    discover._cache.clear()
    yield origin
    stream.clear_preview_urls()
    discover._cache.clear()


@pytest.fixture
def api(cdn):
    engine = new_engine()
    config = ConfigManager(engine=engine)
    provider = _SignedPreviews(cdn)
    registry = FakeRegistry(metadata=[provider, _Loop(cdn, "loop")])

    def _session():
        with Session(engine) as s:
            yield s

    overrides = {
        get_session: _session,
        deps.get_config: lambda: config,
        deps.get_registry: lambda: registry,
        deps.get_auth_manager: lambda: FakeAuthManager(),
    }
    main.app.dependency_overrides.update(overrides)
    client = TestClient(main.app)
    client.provider = provider
    yield client
    for key in overrides:
        main.app.dependency_overrides.pop(key, None)


def test_the_preview_is_served_from_a_fresh_url(api, cdn):
    resp = api.get("/preview/signed/1")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.content == AUDIO
    assert api.provider.resolved == 1


def test_the_requests_of_one_listen_reuse_the_url_just_resolved(api, cdn):
    api.get("/preview/signed/1")
    api.get("/preview/signed/1", headers={"Range": "bytes=100-"})

    assert api.provider.resolved == 1
    assert len(cdn.requests) == 2


def test_a_reused_url_that_expired_is_resolved_again(api, cdn):
    api.get("/preview/signed/1")
    cdn.now += SIGNATURE_S + 1  # the signature ran out, the reuse cache did not

    resp = api.get("/preview/signed/1")

    assert resp.status_code == 200
    assert api.provider.resolved == 2


def test_the_discover_cache_never_hands_out_a_signed_url(api, cdn):
    first = api.get("/discover/trending/tracks").json()["items"][0]
    cdn.now += 3 * SIGNATURE_S  # long after any signature, within the discover cache
    cached = api.get("/discover/trending/tracks").json()["items"][0]

    assert first["preview_url"] == cached["preview_url"] == "/api/preview/signed/1"
    assert cached["playback"] == {"type": "preview", "source": "provider"}
    # What the browser plays still works, whenever it asks.
    assert api.get(cached["preview_url"].removeprefix("/api")).status_code == 200


def test_no_preview_is_a_404(api):
    assert api.get("/preview/signed/none").status_code == 404
    assert api.get("/preview/unknown/1").status_code == 404


def test_a_provider_answering_a_host_url_is_a_404_not_a_loop(api):
    assert api.get("/preview/loop/1").status_code == 404


def test_an_upstream_that_refuses_is_a_provider_failure_not_a_500(api, cdn, monkeypatch):
    monkeypatch.setattr(_SignedCdn, "handle", lambda self, request: httpx.Response(503))
    resp = api.get("/preview/signed/1")
    assert resp.status_code in (502, 503)

