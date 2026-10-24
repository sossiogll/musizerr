"""
Quanto costa passare dal protocollo di chiamata (ADR-0006, fase 2) invece di
chiamare il provider in-process: la stessa chiamata fatta direttamente
sull'istanza e attraverso il proxy loopback (manifest, codec, framing, dispatch
del runner, e di nuovo il codec). Nessun provider reale, nessuna rete: due
provider finti che rispondono subito, così la differenza è tutto overhead.

Misura:

- ``search``: una ricerca di 50 tracce con identificativi, artisti e album;
- ``snapshot``: quanto costa l'istantanea del contesto che ogni chiamata porta
  (le impostazioni del provider e i timeout), con e senza la lettura del DB;
- ``album``: l'arricchimento incrociato di un album di 12 tracce su tre
  provider (``services.enrichment.cross_enrich_album``: una ricerca e una
  lettura dell'album per provider, più l'abbinamento delle tracce).

Uso, dalla cartella ``app/`` (un contenitore usa-e-getta, mai l'host)::

    python -m tests.provider_loopback_benchmark [--repeat 5] [--search 200] [--album 100]

Stampa microsecondi per chiamata (il migliore di ``--repeat`` giri), il costo
diretto, quello attraverso il protocollo e il rapporto.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent.parent))

from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession  # noqa: E402
from musizeer_sdk.context import StaticProviderContext  # noqa: E402
from musizeer_sdk.identifiers import Identifier  # noqa: E402
from musizeer_sdk.interfaces import DownloadProvider, MetadataProvider  # noqa: E402
from musizeer_sdk.models import Album, AlbumRef, ArtistRef, Track  # noqa: E402
from services.enrichment import cross_enrich_album  # noqa: E402
from tests.loopback import loopback_proxy  # noqa: E402

SESSION = AuthSession.anonymous()
TRACKS = 12


def _track(provider: str, index: int, album: str = "Greatest Hits") -> Track:
    track = Track(
        title=f"Song number {index}", artist="The Band", album=album, duration_ms=180_000 + index * 1000,
        cover_url=f"/api/cover/provider/{provider}/c{index}", stream_url=f"/api/stream/provider/{provider}/s{index}",
        provider=provider, provider_id=f"{provider}-{index}", artist_provider_id="ar1", album_provider_id="al1",
        year=1999, track_number=index + 1, disc_number=1,
        albums=[AlbumRef(album, provider, "al1")], artists=[ArtistRef("The Band", provider, "ar1")],
        providers=[provider],
    )
    track.add_id(Identifier(type="isrc", value=f"USABC99{index:05d}"))
    track.add_id(Identifier(type="mbid", value=f"00000000-0000-0000-0000-{index:012d}"))
    track.add_id(Identifier(type="provider_id", value=f"{provider}-{index}", provider=provider))
    return track


def _album(provider: str) -> Album:
    return Album(
        title="Greatest Hits", artist="The Band", year=1999, track_count=TRACKS, provider=provider,
        provider_id="al1", artist_provider_id="ar1", cover_url=f"/api/cover/provider/{provider}/al1",
        tracks=[_track(provider, n) for n in range(TRACKS)], providers=[provider], album_type="album",
    )


class Catalog(MetadataProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name: str) -> None:
        super().__init__(StaticProviderContext(provider_name=name))
        self.name = self.label = name
        self._album = _album(name)

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    # Like a real provider, each answer is built from the service's reply: the
    # models are made on every call (the service's own cost, JSON and network, is
    # not here: it is what the protocol's overhead is small next to).
    def search_tracks(self, query, session, limit=20):
        return [_track(self.name, n) for n in range(limit)]

    def search_albums(self, query, session, limit=20):
        return [_album(self.name)]

    def get_track(self, provider_id, session):
        return _track(self.name, 0)

    def get_album(self, provider_id, session):
        return _album(self.name)

    def implements(self, method):
        # What a proxy answers from the manifest, at no cost: the in-process
        # baseline answers it the same way, so the comparison stays about the calls.
        return True


class Downloader(DownloadProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    name = label = "downloader"
    supported_identifier_types = {"isrc"}

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def download_by_id(self, identifier, workspace, session):
        return None


class Direct:
    """A registry handing out the providers themselves: the in-process baseline."""

    def __init__(self, providers: list[Catalog]) -> None:
        self._providers = providers

    def all_metadata(self):
        return list(self._providers)

    def all_sync(self):
        return []


class Anonymous:
    def authenticate(self, provider):
        return SESSION


def best_microseconds(work: Callable[[], object], loops: int, repeat: int) -> float:
    """The best of ``repeat`` rounds of ``loops`` runs, per run, in microseconds."""
    rounds = []
    for _ in range(repeat):
        start = time.perf_counter()
        for _ in range(loops):
            work()
        rounds.append((time.perf_counter() - start) / loops * 1e6)
    return min(rounds)


UPSTREAM_MS = 50.0


def report(label: str, direct: float, loopback: float, upstream_calls: int = 1) -> None:
    """One line: the cost with and without the protocol, and what the protocol adds
    next to ``upstream_calls`` calls to a service that answers in ``UPSTREAM_MS`` (which
    is what a provider call really waits for, and what these fakes leave out)."""
    upstream = upstream_calls * UPSTREAM_MS * 1000
    print(f"{label:<44} direct {direct:>9.1f} us   protocol {loopback:>9.1f} us   x{loopback / direct:>5.1f}   "
          f"(+{loopback - direct:>8.1f} us = +{(loopback - direct) / (direct + upstream) * 100:>4.1f}% of a call to a "
          f"{UPSTREAM_MS:.0f} ms service)")


def settings_read_cost(repeat: int) -> float | None:
    """The microseconds a context snapshot costs with the provider's settings read
    from a settings table (an in-memory SQLite database)."""
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine

    import db.models  # noqa: F401 — the tables
    from core.settings_resolver import provider_config_from_system_settings
    from db.settings_store import SettingsRepository

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    def read():
        with Session(engine) as session:
            return provider_config_from_system_settings("catalog", SettingsRepository(session).all_system())

    return best_microseconds(read, 300, repeat)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--search", type=int, default=200, help="searches per round")
    parser.add_argument("--album", type=int, default=100, help="album enrichments per round")
    args = parser.parse_args(argv)

    provider = Catalog("catalog")
    proxy = loopback_proxy(lambda _context, built=provider: built, provider.context)

    print(f"search of 50 tracks (3 identifiers, 2 refs, urls each), best of {args.repeat} rounds of {args.search}")
    report("search_tracks(q, 50)",
           best_microseconds(lambda: provider.search_tracks("q", SESSION, 50), args.search, args.repeat),
           best_microseconds(lambda: proxy.search_tracks("q", SESSION, 50), args.search, args.repeat))
    report("get_album (12 tracks)",
           best_microseconds(lambda: provider.get_album("al1", SESSION), args.search, args.repeat),
           best_microseconds(lambda: proxy.get_album("al1", SESSION), args.search, args.repeat))
    report("a declared capability (supports_discography)",
           best_microseconds(lambda: provider.supports_discography(), 20000, args.repeat),
           best_microseconds(lambda: proxy.supports_discography(), 20000, args.repeat))
    report("get_track (one track)",
           best_microseconds(lambda: provider.get_track("1", SESSION), args.search * 5, args.repeat),
           best_microseconds(lambda: proxy.get_track("1", SESSION), args.search * 5, args.repeat))

    downloader = Downloader(StaticProviderContext())
    download_proxy = loopback_proxy(lambda _context, built=downloader: built, downloader.context)
    isrc = Identifier("isrc", "x")
    report("supports_identifier (download resolver)",
           best_microseconds(lambda: downloader.supports_identifier(isrc), 20000, args.repeat),
           best_microseconds(lambda: download_proxy.supports_identifier(isrc), 5000, args.repeat))

    others = [Catalog(name) for name in ("alpha", "beta", "gamma")]
    proxies = [loopback_proxy(lambda _context, built=p: built, p.context) for p in others]
    direct_registry = Direct(others)
    proxy_registry = Direct(proxies)

    def enrich(registry):
        def work():
            album = _album("origin")
            cross_enrich_album(album, registry, Anonymous())
        return work

    print(f"\ncross-enrichment of an album of {TRACKS} tracks on 3 providers, best of {args.repeat} rounds of {args.album}")
    direct = best_microseconds(enrich(direct_registry), args.album, args.repeat)
    through = best_microseconds(enrich(proxy_registry), args.album, args.repeat)
    report("cross_enrich_album (6 upstream calls)", direct, through, upstream_calls=6)

    print("\ncontext snapshot of a call")
    snapshot = best_microseconds(lambda: provider.context.snapshot(), 20000, args.repeat)
    print(f"{'snapshot from memory (tests)':<44} {snapshot:>9.1f} us")
    try:
        read = settings_read_cost(args.repeat)
    except Exception as exc:  # noqa: BLE001 — a benchmark helper, not the host
        print(f"{'settings read from a SQLite table':<44} not measured ({exc})")
    else:
        print(f"{'settings read from a SQLite table (per call)':<44} {read:>9.1f} us")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
