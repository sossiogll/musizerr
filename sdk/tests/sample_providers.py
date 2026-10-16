"""Providers the SDK tests run behind a runner. They use the standard library only."""
from __future__ import annotations

import threading
from typing import Iterator

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizerr_sdk.errors import (
    DelegateHoldsRelease,
    DownloadDeclined,
    DownloadDelegated,
    ProviderRateLimited,
    RemotePlaylistGone,
)
from musizerr_sdk.identifiers import Identifier
from musizerr_sdk.interfaces import (
    DownloadProvider,
    LyricsProvider,
    MetadataProvider,
    StorageProvider,
    StorageStreamResult,
    SyncCandidate,
    SyncProvider,
)
from musizerr_sdk.models import Lyrics, Track
from musizerr_sdk.settings import ProviderSetting

NO_AUTH = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)


def track(title: str = "Song", **kwargs) -> Track:
    return Track(title=title, artist="Artist", album="Album", duration_ms=1000, **kwargs)


class Base:
    label = "Sample"
    auth = NO_AUTH

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session) -> str | None:
        return None


class SampleMetadata(Base, MetadataProvider):
    name = "samplemeta"
    streams_full_tracks = True

    def __init__(self, context) -> None:
        super().__init__(context)
        self.pages_produced = 0
        self.closed = False
        self.seen_settings: list[dict] = []
        self.threads: list[int] = []

    def config_schema(self):
        return [ProviderSetting(key="market", type="string", label="Market", default="IT")]

    def supports_discography(self) -> bool:
        return True

    def supports_isrc_lookup(self) -> bool:
        return True

    def find_by_isrc(self, isrc, session):
        return track("By ISRC", external_ids=[Identifier(type="isrc", value=isrc)])

    def search_tracks(self, query, session, limit=20):
        self.threads.append(threading.get_ident())
        return [track(f"{query} {i}", cover_url=self.context.cover_url(f"c{i}"),
                      stream_url=self.context.stream_url(f"s{i}"), preview_url=self.context.preview_url(f"p{i}"))
                for i in range(limit)]

    def get_track(self, provider_id, session):
        if provider_id == "missing":
            return None
        raise RuntimeError(f"cannot read {provider_id}")

    def read_settings(self, session) -> dict:
        return dict(self.context.settings())

    def get_new_releases(self, session, limit=20):
        self.seen_settings.append({
            "market": self.context.setting("market"), "http": self.context.http_timeout,
            "download": self.context.download_timeout, "connect": self.context.connect_timeout,
            "stream": self.context.stream_start_timeout,
        })
        return []

    def get_saved_tracks(self, session, limit=50, offset=0):
        raise AssertionError("iter_saved_tracks is overridden")

    def iter_saved_tracks(self, session, page_size=50) -> Iterator[list[Track]]:
        try:
            for page in range(5):
                self.pages_produced += 1
                if page == 3 and session.extra.get("explode"):
                    raise ProviderRateLimited("quota", retry_after=12)
                yield [track(f"page {page} {self.context.setting('market')}")]
        finally:
            self.closed = True

    def unlike_saved_track(self, provider_id, session):
        raise PermissionError("missing scope")

    def get_artist(self, provider_id, session):
        raise ProviderRateLimited("slow down", retry_after=7)


class SampleDownload(Base, DownloadProvider):
    name = "sampledl"
    supported_identifier_types = {"isrc"}
    max_concurrent_downloads = 2
    supports_fuzzy = True

    def download_by_id(self, identifier, workspace, session):
        if identifier.value == "delegate":
            raise DownloadDelegated("sampledl", ["queued"], handle="h1")
        if identifier.value == "decline":
            raise DownloadDeclined("sampledl", "have_release", "already there")
        return None

    def download_fuzzy(self, track, workspace, session):
        raise DelegateHoldsRelease("sampledl", "h2", [track], complete=False, release="Album", details=["d"], state={"a": 1})

    def supports_identifier(self, identifier):
        return identifier.storage_type() in self.supported_identifier_types and self.context.setting("never") is None


class SampleLyrics(Base, LyricsProvider):
    name = "samplelyrics"

    def get_lyrics(self, track, session):
        return Lyrics(plain=track.title)


class SampleStorage(Base, StorageProvider):
    name = "samplestorage"
    supports_stream_offset = True

    def __init__(self, context) -> None:
        super().__init__(context)
        self.chunks_closed = False
        self.chunks_produced = 0

    def store(self, source, relative_dest, session):
        return relative_dest

    def exists(self, relative_path, session):
        return True

    def delete(self, relative_path, session):
        return True

    def stream(self, relative_path, workspace, session, offset=0):
        def chunks():
            try:
                for index in range(4):
                    self.chunks_produced += 1
                    if relative_path == "broken" and index == 2:
                        raise ConnectionResetError("connection lost")
                    yield bytes([index]) * 3
            finally:
                self.chunks_closed = True

        return StorageStreamResult(chunks=chunks(), size=12, offset=offset, media_type="audio/mpeg")

    def list_files(self, prefix, session):
        if prefix == "none":
            return None
        return iter([f"{prefix}a.mp3", f"{prefix}b.mp3"])


class SampleSync(Base, SyncProvider, MetadataProvider):
    name = "samplesync"

    def supports_likes(self):
        return True

    def trigger_scan(self, session):
        return None

    def find_candidates(self, track, session):
        return [SyncCandidate(sync_id="s1", track=track)]

    def get_stream_url(self, sync_id, session):
        return f"http://sync/{sync_id}"

    def update_playlist(self, remote_id, name, sync_ids, session):
        raise RemotePlaylistGone("gone")


def session(**extra) -> AuthSession:
    return AuthSession(flow=AuthFlow.NONE, extra=extra)
