"""
The tracks of an artist's whole discography, for "download everything by".

The artist's releases come from ONE provider (``MetadataProvider.
get_artist_albums``, only one that declares ``supports_discography``), are
grouped into releases exactly as the artist page does
(``core.discography.group_releases``: the editions of one release are one
entry) and narrowed to the wanted release types (by default the artist's own
albums, singles and EPs, never compilations or the releases of others the
artist "appears on"). The tracks of each release are read with ``get_album`` —
its representative edition — one release at a time: a provider's rate limit is
a promise to keep, so the work is bounded (``MAX_RELEASES``), a release that
fails is reported and skipped instead of aborting the rest, and a provider that
says it is rate limiting is not asked again in this request.

The listing is read whole, up to ``LISTING_LIMIT`` entries (the provider pages
inside ``get_artist_albums``; a search setting has nothing to do with it): the
releases of the wanted types can only be counted after grouping, so a listing
that reaches the bound is refused like one with too many releases, never cut.

The same song on several releases (the single and the album version) is one
track, the album's: the releases are read album first
(``core.discography.select_releases``) and ``distinct_songs`` keeps the first
copy of each song.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

from core.discography import distinct_songs, group_releases, select_releases
from musizerr_sdk.models import Artist, Track
from core.provider_failures import ProviderFailures
from services.enrichment import fill_missing_artist
from services.errors import classify_error
from services.provider_calls import ProviderCallError, provider_call

logger = logging.getLogger(__name__)

# The most releases one request reads (each is a call to the provider). A
# discography with more is refused, not cut: the caller narrows the release
# types.
MAX_RELEASES = 60

# The most entries (editions included) read from the provider's listing, which
# it pages itself. Our bound, not a search setting: the listing must be whole
# to count the releases of the chosen types honestly. A provider with pages of
# ten (20 list calls here) is the expensive case.
LISTING_LIMIT = 200

# Stable codes of the refusals (``detail.error_code``), translated by the UI.
NOT_SUPPORTED = "discography_not_supported"
EMPTY = "discography_empty"
TOO_LARGE = "discography_too_large"


class DiscographyError(Exception):
    """A discography that cannot be gathered, for a reason the caller can act
    on: ``code`` is stable, ``message`` readable, ``extra`` the numbers."""

    def __init__(self, code: str, message: str, **extra: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def detail(self) -> dict:
        return {"error_code": self.code, "error_message": self.message, **self.extra}


@dataclass
class Discography:
    # The songs to download, album versions first, each once.
    tracks: list[Track]
    # The releases whose tracks were read, and those that could not be.
    releases: int
    releases_failed: int = 0
    # Why the ones that failed did.
    failures: ProviderFailures = field(default_factory=ProviderFailures)


def not_supported(provider: str) -> DiscographyError:
    return DiscographyError(NOT_SUPPORTED, f"Provider '{provider}' cannot list the releases of an artist with their tracks.")


def gather_discography(
    artist: Artist, provider, auth_session, *, priority: list[str], release_types: Iterable[str],
) -> Discography:
    """The songs of ``artist`` on ``provider`` (see the module docstring).
    Raises :class:`DiscographyError` when there is nothing to take or too much,
    ``ProviderCallError`` when the provider fails before any release was read."""
    with provider_call(provider.name):
        albums = provider.get_artist_albums(artist.provider_id, auth_session, limit=LISTING_LIMIT)
    fill_missing_artist(albums, artist.name, artist.provider_id)
    releases = select_releases(group_releases(albums, priority), release_types)
    if len(albums) >= LISTING_LIMIT:
        # The listing may go on: the releases of the chosen types cannot be counted.
        raise DiscographyError(
            TOO_LARGE,
            f"The artist has more releases than the {LISTING_LIMIT} one request lists: choose fewer release types.",
            releases=len(releases), max_releases=MAX_RELEASES,
        )
    if len(releases) > MAX_RELEASES:
        raise DiscographyError(
            TOO_LARGE,
            f"The artist has {len(releases)} releases of the chosen types: more than the {MAX_RELEASES} one request takes.",
            releases=len(releases), max_releases=MAX_RELEASES,
        )
    if not releases:
        raise DiscographyError(EMPTY, "The artist has no release of the chosen types.")

    failures = ProviderFailures()
    read: list[list[Track]] = []
    failed = 0
    last_error: BaseException | None = None
    for position, release in enumerate(releases):
        try:
            album = provider.get_album(release.representative.provider_id, auth_session)
        except Exception as exc:  # noqa: BLE001 — a release does not stop the discography
            logger.warning("get_album %s/%s failed: %s", provider.name, release.representative.provider_id, exc)
            failures.record(provider.name, exc)
            last_error = exc
            failed += 1
            if classify_error(exc) == "rate_limited":
                failed += len(releases) - position - 1
                break
            continue
        if album is None:
            failed += 1
            continue
        read.append(album.tracks)

    if not read and last_error is not None:
        raise ProviderCallError(provider.name, last_error) from last_error
    tracks = distinct_songs(track for album_tracks in read for track in album_tracks)
    if not tracks:
        raise DiscographyError(EMPTY, "The releases of the artist have no track to download.")
    return Discography(tracks=tracks, releases=len(read), releases_failed=failed, failures=failures)
