from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Iterable
import re

from musizerr_sdk.identifiers import ISRC, MBID, UNIVERSAL_IDENTIFIER_TYPES
from musizerr_sdk.models import Album, Artist, Track
from musizerr_sdk.text import DASH_TAIL_RE, PARENS_RE, normalize, strip_version_suffix, strip_video_noise

from .identifiers import identity_rank

# Two durations are the same recording within 3 seconds, or 2% of the
# longer one for long tracks (rounding of catalogs that give whole seconds,
# a few seconds of silence more or less in a file).
_DURATION_TOLERANCE_MS = 3000
_DURATION_TOLERANCE_RATIO = 0.02
# The same release dated one year apart by two sources (a digital reissue
# dated when it went online): tolerated only when album and duration agree.
_TRACK_YEAR_TOLERANCE = 1


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    confidence: str = "none"
    reason: str = ""


def match_track(source: Track, candidate: Track) -> MatchResult:
    """Whether two tracks are the same recording.

    Exact: a shared ISRC, a shared MBID (same year: the MBID of a recording
    spans its remasters), or the same item of the same provider (a shared
    provider id, including a sync id). Otherwise the identity of the title
    (base title AND version, see ``version_identity``) and one artist in
    common are required, and album, year and duration must not disagree:
    a different album, a year off by more than one (one only when album and
    duration agree), or a duration beyond the tolerance rejects. At least
    one of them must confirm."""
    shared_isrc = shared_track_identity(source, candidate, ISRC)
    if shared_isrc:
        return MatchResult(True, confidence="exact", reason="shared_isrc")

    shared_mbid = shared_track_identity(source, candidate, MBID)
    if shared_mbid:
        source_year = getattr(source, "year", None)
        candidate_year = getattr(candidate, "year", None)
        if source_year is not None and candidate_year is not None and source_year != candidate_year:
            return MatchResult(False, reason="mbid_year_mismatch")
        return MatchResult(True, confidence="exact", reason="shared_mbid")

    if _shared_provider_identifier(source, candidate):
        return MatchResult(True, confidence="exact", reason="shared_provider_id")

    source_title = getattr(source, "title", "") or ""
    candidate_title = getattr(candidate, "title", "") or ""
    if base_title_key(source_title) != base_title_key(candidate_title):
        return MatchResult(False, reason="title_mismatch")
    if version_identity(source_title) != version_identity(candidate_title):
        return MatchResult(False, reason="version_mismatch")
    if not _artists_match(source, candidate):
        return MatchResult(False, reason="artist_mismatch")

    confirmations = 0

    source_album = normalize(getattr(source, "album", "") or "")
    candidate_album = normalize(getattr(candidate, "album", "") or "")
    album_agrees = bool(source_album and candidate_album and source_album == candidate_album)
    if source_album and candidate_album:
        if not album_agrees:
            return MatchResult(False, reason="album_mismatch")
        confirmations += 1

    source_duration = getattr(source, "duration_ms", 0) or 0
    candidate_duration = getattr(candidate, "duration_ms", 0) or 0
    duration_agrees = False
    if source_duration and candidate_duration:
        if not _duration_close(source_duration, candidate_duration):
            return MatchResult(False, reason="duration_mismatch")
        duration_agrees = True
        confirmations += 1

    source_year = getattr(source, "year", None)
    candidate_year = getattr(candidate, "year", None)
    if source_year is not None and candidate_year is not None:
        gap = abs(source_year - candidate_year)
        if gap == 0:
            confirmations += 1
        elif not (gap <= _TRACK_YEAR_TOLERANCE and album_agrees and duration_agrees):
            return MatchResult(False, reason="year_mismatch")

    if confirmations == 0:
        return MatchResult(False, reason="missing_track_confirmations")

    return MatchResult(True, confidence="strong", reason="title_artist_plus_metadata")


def find_match(source: Track, candidates: Iterable[Track]) -> Track | None:
    """
    Primo candidato che corrisponde a ``source`` secondo ``match_track``.
    Primitiva riusabile: i provider (es. la disponibilità nel sync) la usano per
    non duplicare il ciclo di selezione — stessa logica di match ovunque.
    """
    for candidate in candidates:
        if match_track(source, candidate).matched:
            return candidate
    return None


def requested_track_in_package(requested: Track, package_tracks: Iterable[Track]) -> bool:
    """Whether ``requested`` is one of ``package_tracks``, the tracks an external
    system holds for the release (album) a download was delegated for. The
    release was chosen for ``requested``, so a package track that names no album is
    of its album: the title the system gives the release can differ in form
    from the requested one, and a missing album would leave the matcher
    without the confirmation it relies on. Paired as the tracks of two
    editions of one album are (``pair_album_tracks``)."""
    candidates = [track if track.album else replace(track, album=requested.album) for track in package_tracks]
    return bool(pair_album_tracks([requested], candidates))


def pair_album_tracks(sources: list[Track], candidates: list[Track]) -> dict[int, int]:
    """The tracks of two editions of an ALREADY matched album, paired one to
    one: ``{source index: candidate index}``.

    A pair needs the same title identity (base title and version). It is
    eligible when ``match_track`` accepts it, or when the title identity
    appears once on each side: the album context then stands in for the
    missing album/year/duration, artists may be credited differently and a
    duration may drift (a hidden track at the end). When several tracks
    share the title identity, never by title alone: a duration beyond the
    tolerance rejects and match_track or the position must agree. Among
    eligible pairs the best go first: accepted by ``match_track``, then the
    same disc/track number, then the closest duration; ties keep the album
    order. Every track is used at most once."""
    source_keys = [title_identity_key(t.title or "") for t in sources]
    candidate_keys = [title_identity_key(t.title or "") for t in candidates]
    source_count: dict[str, int] = {}
    for key in source_keys:
        source_count[key] = source_count.get(key, 0) + 1
    candidate_count: dict[str, int] = {}
    for key in candidate_keys:
        candidate_count[key] = candidate_count.get(key, 0) + 1

    ranked = []
    for s, source in enumerate(sources):
        for c, candidate in enumerate(candidates):
            if not source_keys[s] or source_keys[s] != candidate_keys[c]:
                continue
            source_duration = source.duration_ms or 0
            candidate_duration = candidate.duration_ms or 0
            unique = source_count[source_keys[s]] == 1 and candidate_count[candidate_keys[c]] == 1
            if not unique and not _duration_close(source_duration, candidate_duration):
                continue
            strong = match_track(source, candidate).matched
            same_position = _same_position(source, candidate)
            if not (strong or unique or same_position):
                continue
            gap = abs(source_duration - candidate_duration) if source_duration and candidate_duration else None
            ranked.append(((not strong, not same_position, gap is None, gap or 0, s, c), s, c))

    pairs: dict[int, int] = {}
    used: set[int] = set()
    for _rank, s, c in sorted(ranked):
        if s in pairs or c in used:
            continue
        pairs[s] = c
        used.add(c)
    return pairs


def _same_position(source: Track, candidate: Track) -> bool:
    """Same track number (and disc, when both know it)."""
    if source.track_number is None or candidate.track_number is None:
        return False
    if source.track_number != candidate.track_number:
        return False
    return source.disc_number is None or candidate.disc_number is None or source.disc_number == candidate.disc_number
def match_album(source: Album, candidate: Album) -> MatchResult:
    shared_mbid = _shared_identifier(source, candidate, MBID)
    if shared_mbid:
        return MatchResult(True, confidence="exact", reason="shared_mbid")

    if normalize(getattr(source, "title", "")) != normalize(getattr(candidate, "title", "")):
        return MatchResult(False, reason="title_mismatch")

    source_artist = normalize(getattr(source, "artist", "") or "")
    candidate_artist = normalize(getattr(candidate, "artist", "") or "")
    if not source_artist or not candidate_artist:
        return MatchResult(False, reason="missing_album_artist")
    if source_artist != candidate_artist:
        return MatchResult(False, reason="artist_mismatch")

    source_year = getattr(source, "year", None)
    candidate_year = getattr(candidate, "year", None)
    if source_year is None or candidate_year is None:
        return MatchResult(False, reason="missing_album_year")
    if source_year != candidate_year:
        return MatchResult(False, reason="year_mismatch")

    source_track_count = _album_track_count(source)
    candidate_track_count = _album_track_count(candidate)
    if (
        source_track_count is not None
        and candidate_track_count is not None
        and source_track_count != candidate_track_count
    ):
        return MatchResult(False, reason="track_count_mismatch")

    if source_track_count is not None and candidate_track_count is not None:
        return MatchResult(True, confidence="strong", reason="title_artist_year_track_count")

    return MatchResult(True, confidence="strong", reason="title_artist_year")



# Marcatori di una edizione della stessa uscita (deluxe, rimasterizzata,
# regionale...): con uno di questi nella coda del titolo, l'anno può
# differire dall'originale.
_EDITION_RE = re.compile(
    r"deluxe|remaster|expanded|anniversary|edition|bonus|special|reissue|version|explicit|clean",
    re.I,
)
_RELEASE_YEAR_TOLERANCE = 1


def release_title_key(title: str) -> str:
    """Chiave di confronto del titolo di un'uscita: titolo base (senza coda
    di versione) normalizzato. Un titolo di sola punteggiatura (es. "?!")
    normalizzato sarebbe vuoto e uguale a ogni altro titolo di sola
    punteggiatura: in quel caso conta la punteggiatura stessa, senza spazi."""
    base = strip_version_suffix(title)
    return normalize(base) or re.sub(r"\s+", "", base).casefold()


def is_edition_title(title: str) -> bool:
    """True se il titolo porta un marcatore di edizione nella sua coda di
    versione (es. "Album (Deluxe)", "Album - 2011 Remaster")."""
    version_parts = PARENS_RE.findall(title) + DASH_TAIL_RE.findall(title)
    return any(_EDITION_RE.search(part) for part in version_parts)


_VARIANT_RE = re.compile(r"\b(live|remix(?:es|ed)?|acoustic|instrumental)\b", re.I)


def _variants(album: Album) -> frozenset[str]:
    """Le varianti che fanno di un album un'uscita diversa dall'originale con
    lo stesso titolo base (live, remix, acustica, strumentale)."""
    version_parts = PARENS_RE.findall(album.title) + DASH_TAIL_RE.findall(album.title)
    found = {match.lower() for part in version_parts for match in _VARIANT_RE.findall(part)}
    variants = {"remix" if name.startswith("remix") else name for name in found}
    if album.album_type == "live":
        variants.add("live")
    return frozenset(variants)


def match_release(source: Album, candidate: Album) -> MatchResult:
    """Se due album sono edizioni della stessa uscita (per raggruppare una
    discografia): stesso MBID, oppure stesso titolo base (senza coda di
    versione), artista in comune e anno entro la tolleranza. Un anno diverso
    è ammesso quando uno dei due titoli è un'edizione. Tipi diversi e noti
    (es. single e album, compilation) e varianti diverse (live, remix,
    acustica, strumentale) non si uniscono mai."""
    if _shared_identifier(source, candidate, MBID):
        return MatchResult(True, confidence="exact", reason="shared_mbid")

    if release_title_key(source.title) != release_title_key(candidate.title):
        return MatchResult(False, reason="title_mismatch")
    if not _artists_match(source, candidate):
        return MatchResult(False, reason="artist_mismatch")

    if source.album_type and candidate.album_type and source.album_type != candidate.album_type:
        return MatchResult(False, reason="release_type_mismatch")
    if _variants(source) != _variants(candidate):
        return MatchResult(False, reason="release_type_mismatch")

    if is_edition_title(source.title) or is_edition_title(candidate.title):
        return MatchResult(True, confidence="strong", reason="edition_of_release")
    if source.year is None or candidate.year is None:
        return MatchResult(True, confidence="weak", reason="title_artist")
    if abs(source.year - candidate.year) > _RELEASE_YEAR_TOLERANCE:
        return MatchResult(False, reason="year_mismatch")
    return MatchResult(True, confidence="strong", reason="title_artist_year")


def match_artist(source: Artist, candidate: Artist) -> MatchResult:
    shared_mbid = _shared_identifier(source, candidate, MBID)
    if shared_mbid:
        return MatchResult(True, confidence="exact", reason="shared_mbid")

    if normalize(getattr(source, "name", "")) != normalize(getattr(candidate, "name", "")):
        return MatchResult(False, reason="name_mismatch")

    return MatchResult(True, confidence="strong", reason="normalized_name")


def match_artist_name(name: str, candidate) -> MatchResult:
    return match_artist(SimpleNamespace(name=name), candidate)


def shared_track_identity(source, candidate, *id_types: str) -> str | None:
    return _shared_identifier(source, candidate, *id_types)


def _shared_identifier(source, candidate, *id_types: str) -> str | None:
    source_ids = _identifier_values(source, id_types)
    candidate_ids = _identifier_values(candidate, id_types)
    shared = source_ids & candidate_ids
    return next(iter(shared), None) if shared else None


_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|;|/|&|\bfeat\.?\b|\bft\.?\b|\bwith\b)\s*", re.I)


def artist_names(item) -> set[str]:
    """Insieme normalizzato di TUTTI gli artisti: lista strutturata ``artists``
    più il campo primario ``artist`` (che può contenere più artisti separati da
    virgola/feat/&). Provider-agnostico."""
    names: set[str] = set()
    for ref in getattr(item, "artists", None) or []:
        n = normalize(getattr(ref, "name", "") or "")
        if n:
            names.add(n)
    for part in _ARTIST_SPLIT_RE.split(getattr(item, "artist", "") or ""):
        n = normalize(part)
        if n:
            names.add(n)
    return names


def _artists_match(source, candidate) -> bool:
    """Match se gli insiemi di artisti si intersecano (almeno un artista in
    comune) — copre i 'feat.' dove un lato elenca meno artisti dell'altro."""
    src = artist_names(source)
    cand = artist_names(candidate)
    return bool(src and cand and (src & cand))


def _identifier_values(item, id_types: Iterable[str]) -> set[str]:
    external_ids = getattr(item, "external_ids", None)
    if external_ids is None:
        return set()
    type_set = set(id_types)
    return {
        identifier.value
        for identifier in external_ids
        if identifier.type in type_set
    }


# Parentesi/quadre (es. "(feat. X)", "(Remastered)", "[Bonus Track]") e coda
# dopo un trattino spaziato (es. "- 2012 Remaster", "- Rap Version", "- Live"):
# PARENS_RE e DASH_TAIL_RE (musizerr_sdk.text) li trovano, questo ne legge il contenuto.
_PARENS_CONTENT_RE = re.compile(r"[\(\[](.*?)[\)\]]")


def base_title_key(title: str) -> str:
    """The base title (no version tail), normalized: the key that narrows
    candidates before ``match_track`` decides. Never an identity by itself:
    the remixes of a song share it (see ``title_identity_key``)."""
    return normalize(strip_version_suffix(title or ""))


# A tail that makes another recording of the same song: remix, live, mix,
# edit, acoustic... Credits (feat.), edition markers (remaster, year,
# explicit) and the decoration of a video title (official video, HD, lyrics)
# are not versions.
_FEAT_RE = re.compile(r"^\s*(?:feat\b|ft\b|featuring\b|with\b|prod\b|produced by\b)", re.I)
_VERSION_MARKER_RE = re.compile(
    r"\b(?:re-?mix(?:es|ed)?|rmx|mix(?:ed)?|live|acoustic|unplugged|instrumental|karaoke|demo|edit|rework|dub"
    r"|version|reprise|session|acapella|cappella|orchestral|stripped|extended|club|bootleg|mash-?up|cover|vip)\b",
    re.I,
)
_EDITION_WORDS_RE = re.compile(
    r"\b(?:\d{4}|remaster(?:ed)?|digital(?:ly)?|deluxe|edition|explicit|clean|bonus|track|mono|stereo)\b", re.I,
)
# What is left of a tail that only names the original recording.
_PLAIN_VERSIONS = frozenset({"", "version", "original", "album version", "original version", "original mix"})


def version_identity(title: str) -> frozenset[str]:
    """The versions a title names in its tails (parentheses, brackets, the
    part after " - "), normalized without edition words: "Song - Federico
    Scavo Remix" and "Song (Federico Scavo Remix)" give {"federico scavo
    remix"}, "Song (feat. X) - 2011 Remaster" gives nothing. Part of the
    identity of a track: two versions of one song never match."""
    title = strip_video_noise(title or "")
    parts = _PARENS_CONTENT_RE.findall(title) + DASH_TAIL_RE.findall(PARENS_RE.sub("", title))
    versions = set()
    for part in parts:
        part = part.strip().lstrip("-").strip()
        if _FEAT_RE.match(part) or not _VERSION_MARKER_RE.search(part):
            continue
        token = re.sub(r"\s+", " ", _EDITION_WORDS_RE.sub(" ", normalize(part))).strip()
        if token not in _PLAIN_VERSIONS:
            versions.add(token)
    return frozenset(versions)


def title_identity_key(title: str) -> str:
    """Base title and versions in one key: the same for the same recording
    whatever the punctuation of its tails, different for two versions. What
    a count "by song" groups by (coverage), never a bare base title."""
    versions = version_identity(title)
    base = base_title_key(title)
    return f"{base} | {' | '.join(sorted(versions))}" if versions else base


SONG_KEY_PREFIX = "song:"


def song_keys(track: Track) -> frozenset[str]:
    """The names of a song that stay the same whichever record or source it
    comes from, for a decision that must outlive a record id (a favorite the
    user took off): every stable identifier it carries (universal ids and
    provider ids, never a sync id: it names a file and changes on a rescan)
    as ``<storage type>:<value>``, and ``song:<title identity>|<artist>`` for
    each of its artists (the same title identity and one artist in common, as
    ``match_track`` asks). Two tracks sharing a key are the same song."""
    keys = {
        f"{identifier.storage_type()}:{identifier.value}"
        for identifier in getattr(track, "external_ids", None) or []
        if identity_rank(identifier.storage_type()) < 2 and identifier.value
    }
    title = title_identity_key(getattr(track, "title", "") or "")
    if title:
        keys.update(f"{SONG_KEY_PREFIX}{title}|{name}" for name in artist_names(track))
    return frozenset(keys)


def same_song(source: Track, candidate: Track) -> bool:
    """Whether two tracks are the same song whichever release carries it (the
    single and the album version of one recording): they share a name in
    ``song_keys`` (a stable identifier, or the title identity with one artist
    in common) and their durations agree. Unlike ``match_track`` it asks no
    confirmation from album or year — the releases differ by definition — and
    the duration is what keeps two different songs with one title apart."""
    return bool(song_keys(source) & song_keys(candidate)) and _duration_close(
        getattr(source, "duration_ms", 0) or 0, getattr(candidate, "duration_ms", 0) or 0,
    )


def duration_tolerance_ms(a_ms: int, b_ms: int) -> int:
    """How far two durations of the same recording can be: 3 seconds, or 2%
    of the longer one."""
    return max(_DURATION_TOLERANCE_MS, int(max(a_ms, b_ms) * _DURATION_TOLERANCE_RATIO))


def _duration_close(a_ms: int, b_ms: int) -> bool:
    """Two durations are the same within the tolerance; an unknown one (0)
    neither confirms nor rejects."""
    if a_ms == 0 or b_ms == 0:
        return True
    return abs(a_ms - b_ms) <= duration_tolerance_ms(a_ms, b_ms)


def _provider_identifier_pairs(item) -> set[tuple[str, str]]:
    """(storage type, value) of every non-universal identifier of an item:
    its provider ids and sync ids."""
    return {
        (identifier.storage_type(), identifier.value)
        for identifier in getattr(item, "external_ids", None) or []
        if identifier.type not in UNIVERSAL_IDENTIFIER_TYPES
    }


def _shared_provider_identifier(source, candidate) -> bool:
    """The same item of the same provider (same id type and value)."""
    return bool(_provider_identifier_pairs(source) & _provider_identifier_pairs(candidate))


def _album_track_count(item) -> int | None:
    track_count = getattr(item, "track_count", None)
    if track_count is not None:
        return track_count
    tracks = getattr(item, "tracks", None)
    if tracks:
        return len(tracks)
    return None
