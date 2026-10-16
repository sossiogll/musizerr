"""
Raggruppamento di una discografia in uscite: le edizioni della stessa
uscita (deluxe, rimasterizzata, regionale, la stessa da un altro provider)
diventano una voce sola con la lista ``editions``. Nessuna edizione va
persa: ognuna resta raggiungibile col proprio provider e id.

Ordine della lista: per tipo di uscita (l'ordine di ``ALBUM_TYPES``: album,
single, ep, compilation, live, appears_on; tipo ignoto in fondo), poi per
anno decrescente (l'anno di un'uscita è quello della sua edizione più
vecchia; senza anno in fondo), poi per titolo e, a parità, nell'ordine in
cui la discografia le presentava.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from musizerr_sdk.identifiers import Identifier
from .matching import is_edition_title, match_release, release_title_key, same_song, song_keys
from musizerr_sdk.models import ALBUM_TYPES, Album, Track
from .utils import provider_sort_key


# How many releases (editions included, before grouping) the artist page asks
# a provider for. A bound of ours, never a search setting: a provider pages
# inside ``get_artist_albums`` until it has this many or runs out. An artist
# with more shows the first ones and the page says the list may be longer.
RELEASES_LISTING_LIMIT = 100

# The releases of a discography download by default: the artist's own albums,
# singles and EPs. Compilations, live recordings and the releases of others the
# artist only "appears on" repeat or belong to someone else's work.
OWN_RELEASE_TYPES = ("album", "single", "ep")

# What a release whose provider gives no type counts as when choosing which to
# take (a catalog of albums only).
UNTYPED_RELEASE = "album"


@dataclass
class ReleaseGroup:
    representative: Album
    editions: list[Album] = field(default_factory=list)

    @property
    def album_type(self) -> str | None:
        return next((a.album_type for a in [self.representative, *self.editions] if a.album_type), None)

    @property
    def year(self) -> int | None:
        """L'anno dell'uscita: quello della sua edizione più vecchia."""
        return min((a.year for a in self.editions if a.year), default=None)

    @property
    def external_ids(self) -> list[Identifier]:
        merged: dict[tuple, Identifier] = {}
        for album in [self.representative, *self.editions]:
            for identifier in album.external_ids:
                merged.setdefault((identifier.type, identifier.value, identifier.provider), identifier)
        return list(merged.values())


def group_releases(albums: list[Album], provider_priority: list[str]) -> list[ReleaseGroup]:
    """Un gruppo per uscita, nell'ordine documentato in testa al modulo.
    Il rappresentante di ogni gruppo è l'edizione del provider con priorità
    più alta; a parità, quella senza marcatore di edizione e poi la più
    vecchia."""
    first_seen = {id(album): index for index, album in enumerate(albums)}

    def representative_key(album: Album):
        return provider_sort_key(album.provider, provider_priority), is_edition_title(album.title), album.year or 9999

    groups: list[ReleaseGroup] = []
    for album in sorted(albums, key=representative_key):
        group = next((g for g in groups if _belongs(album, g)), None)
        if group is None:
            groups.append(ReleaseGroup(representative=album, editions=[album]))
        else:
            group.editions.append(album)

    groups = _merge_redated_editions(groups, representative_key)
    groups.sort(key=lambda g: _release_order(g, min(first_seen[id(a)] for a in g.editions)))
    return groups


def _release_order(group: ReleaseGroup, first_seen: int) -> tuple:
    album_type = group.album_type
    type_rank = ALBUM_TYPES.index(album_type) if album_type in ALBUM_TYPES else len(ALBUM_TYPES)
    year = group.year
    return (type_rank, year is None, -(year or 0), release_title_key(group.representative.title), first_seen)


def _merge_redated_editions(groups: list[ReleaseGroup], representative_key) -> list[ReleaseGroup]:
    """Unisce la stessa uscita datata diversamente da due provider (tipico:
    una ristampa digitale che porta la data di pubblicazione online invece
    di quella originale), che ``match_release`` separa per l'anno.

    Solo fra gruppi con stesso titolo, artista, tipo noto e varianti, senza
    marcatori di edizione, e solo quando ogni provider ne elenca UNA sola:
    se un provider ha due uscite omonime (es. due album omonimi di anni
    diversi), sono uscite distinte e nessuna si unisce oltre la tolleranza
    d'anno. Un numero di tracce noto e diverso le tiene separate."""
    clusters: list[list[ReleaseGroup]] = []
    for group in groups:
        cluster = next((c for c in clusters if _redated_candidates(c[0], group)), None)
        if cluster is None:
            clusters.append([group])
        else:
            cluster.append(group)

    merged: list[ReleaseGroup] = []
    for cluster in clusters:
        if len(cluster) == 1 or not _one_release_per_provider(cluster) or not _same_track_count(cluster):
            merged.extend(cluster)
            continue
        editions = [album for group in cluster for album in group.editions]
        merged.append(ReleaseGroup(representative=min(editions, key=representative_key), editions=editions))
    return merged


def _redated_candidates(a: ReleaseGroup, b: ReleaseGroup) -> bool:
    first, second = a.representative, b.representative
    if not first.album_type or first.album_type != second.album_type:
        return False
    if is_edition_title(first.title) or is_edition_title(second.title):
        return False
    return match_release(first, second).reason == "year_mismatch"


def _one_release_per_provider(cluster: list[ReleaseGroup]) -> bool:
    seen: set[str] = set()
    for group in cluster:
        providers = {album.provider for album in group.editions}
        if providers & seen:
            return False
        seen |= providers
    return True


def _same_track_count(cluster: list[ReleaseGroup]) -> bool:
    counts = {
        album.track_count
        for group in cluster for album in group.editions
        if album.track_count and not is_edition_title(album.title)
    }
    return len(counts) <= 1


# Esiti che dicono "uscite diverse", non solo "non riconosciuta".
_CONFLICTS = frozenset({"year_mismatch", "release_type_mismatch"})


def _belongs(album: Album, group: ReleaseGroup) -> bool:
    """``album`` è un'edizione di almeno un membro del gruppo e non ne
    contraddice nessuno: una voce senza anno, o marcata come edizione, non
    unisce due uscite distinte che hanno lo stesso titolo."""
    results = [match_release(member, album) for member in group.editions]
    return any(r.matched for r in results) and not any(r.reason in _CONFLICTS for r in results)


def select_releases(groups: list[ReleaseGroup], types: Iterable[str]) -> list[ReleaseGroup]:
    """The releases of ``types`` (``ALBUM_TYPES`` names; a release with no type
    counts as ``UNTYPED_RELEASE``), in the order a discography is read so that
    the album's version of a song comes before the single's: by type (the
    order of ``ALBUM_TYPES``), then the oldest first (no year last) — the
    original release of a song before its later appearances."""
    wanted = set(types)
    chosen = [g for g in groups if (g.album_type or UNTYPED_RELEASE) in wanted]
    return sorted(
        chosen,
        key=lambda g: (ALBUM_TYPES.index(g.album_type or UNTYPED_RELEASE), g.year is None, g.year or 0),
    )


def distinct_songs(tracks: Iterable[Track]) -> list[Track]:
    """``tracks`` without the repeats of one song (``core.matching.same_song``),
    the first of each kept, in order: read the releases album first and the
    single's copy of a song is dropped, the album's stays."""
    kept: list[Track] = []
    by_key: dict[str, list[Track]] = {}
    for track in tracks:
        keys = song_keys(track)
        if any(same_song(track, other) for key in keys for other in by_key.get(key, ())):
            continue
        kept.append(track)
        for key in keys:
            by_key.setdefault(key, []).append(track)
    return kept
