"""
Discografia raggruppata per uscita (``core/discography.py``,
``core.matching.match_release``): edizioni deluxe, rimasterizzate e
regionali, e la stessa uscita da due provider, diventano una voce con
``editions``; compilation, live e titoli solo simili restano separati.
Nessuna edizione va persa e il rappresentante segue la priorità.
"""
from __future__ import annotations

import pytest

from core.discography import group_releases
from musizerr_sdk.identifiers import MBID, PROVIDER_ID, Identifier
from core.matching import is_edition_title, match_release
from musizerr_sdk.models import Album
from core.serialization import serialize_release_group


def _album(title, *, provider="pa", pid=None, year=2017, artist="Caparezza", album_type="album", ids=()):
    album = Album(title=title, artist=artist, year=year, provider=provider,
                  provider_id=pid or f"{provider}-{title}", album_type=album_type)
    album.add_id(Identifier(type=PROVIDER_ID, value=album.provider_id, provider=provider))
    for identifier in ids:
        album.add_id(identifier)
    return album


def _titles(groups):
    return [sorted(a.title for a in g.editions) for g in groups]


@pytest.mark.parametrize("title", [
    "Prisoner 709 (Deluxe)",
    "Prisoner 709 - Remastered 2021",
    "Prisoner 709 (Italian Version)",
    "Prisoner 709 (20th Anniversary Edition)",
])
def test_editions_merge_with_the_original_even_years_later(title):
    original = _album("Prisoner 709", year=2017)
    edition = _album(title, pid="e", year=2021)

    assert match_release(original, edition).matched


@pytest.mark.parametrize("a, b", [
    ("Habemus Capa", "Habemus Capa Live"),
    ("Verità supposte", "Verità"),
    ("Le dimensioni del mio caos", "Le dimensioni del tuo caos"),
])
def test_near_miss_titles_do_not_merge(a, b):
    assert not match_release(_album(a), _album(b, pid="x")).matched


def test_a_live_album_is_not_the_studio_album():
    assert not match_release(_album("Museica"), _album("Museica (Live)", pid="l")).matched
    assert not match_release(_album("Museica"), _album("Museica", pid="l", album_type="live")).matched


def test_a_compilation_is_not_the_studio_album():
    assert not match_release(_album("Habemus Capa"), _album("Habemus Capa", pid="c", album_type="compilation")).matched


def test_same_title_years_apart_without_an_edition_marker_do_not_merge():
    assert not match_release(_album("Caparezza", year=2000), _album("Caparezza", pid="y", year=2010)).matched


def test_a_shared_mbid_always_merges():
    mbid = Identifier(type=MBID, value="rg-1")
    assert match_release(_album("A", ids=[mbid]), _album("Totally Different", pid="z", ids=[mbid])).matched


def test_edition_markers_are_read_only_in_the_version_tail():
    assert is_edition_title("Album (Deluxe Edition)")
    assert not is_edition_title("Special Delivery")


def test_a_discography_groups_into_releases_and_keeps_every_edition():
    albums = [
        _album("Prisoner 709", provider="pb", pid="pb-1", year=2017),
        _album("Prisoner 709 (Deluxe)", provider="pa", pid="pa-1", year=2017),
        _album("Prisoner 709", provider="pa", pid="pa-2", year=2017),
        _album("Habemus Capa", provider="pa", pid="pa-3", year=2006),
        _album("Habemus Capa (Remastered)", provider="pa", pid="pa-4", year=2016),
        _album("Il meglio di Caparezza", provider="pa", pid="pa-5", year=2012, album_type="compilation"),
        _album("Compilation di tutti", provider="pa", pid="pa-6", year=2015, album_type="appears_on", artist="Vari, Caparezza"),
        _album("Habemus Capa Live", provider="pa", pid="pa-7", year=2008, album_type="live"),
    ]

    groups = group_releases(albums, ["pa", "pb"])

    assert _titles(groups) == [
        ["Prisoner 709", "Prisoner 709", "Prisoner 709 (Deluxe)"],
        ["Habemus Capa", "Habemus Capa (Remastered)"],
        ["Il meglio di Caparezza"],
        ["Habemus Capa Live"],
        ["Compilation di tutti"],
    ]
    assert sum(len(g.editions) for g in groups) == len(albums)


def test_the_representative_follows_provider_priority_then_prefers_the_plain_edition():
    albums = [
        _album("Prisoner 709", provider="pb", pid="pb-1"),
        _album("Prisoner 709 (Deluxe)", provider="pa", pid="pa-1"),
        _album("Prisoner 709", provider="pa", pid="pa-2"),
    ]

    assert group_releases(albums, ["pa", "pb"])[0].representative.provider_id == "pa-2"
    assert group_releases(albums, ["pb", "pa"])[0].representative.provider_id == "pb-1"


def test_the_serialized_group_carries_editions_merged_ids_and_type():
    mbid = Identifier(type=MBID, value="rg-9")
    albums = [
        _album("Prisoner 709", provider="pa", pid="pa-1", album_type=None),
        _album("Prisoner 709", provider="pb", pid="pb-1", ids=[mbid]),
    ]

    data = serialize_release_group(group_releases(albums, ["pa", "pb"])[0])

    assert data["provider_id"] == "pa-1"
    assert data["album_type"] == "album"
    assert {(e["provider"], e["provider_id"]) for e in data["editions"]} == {("pa", "pa-1"), ("pb", "pb-1")}
    ids = {(i["type"], i["value"]) for i in data["external_ids"]}
    assert ("mbid", "rg-9") in ids


# ---------------------------------------------------------------------------
# La rotta: paginazione sulle uscite raggruppate
# ---------------------------------------------------------------------------

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.interfaces import MetadataProvider
from musizerr_sdk.models import Artist
from musizerr_sdk.context import StaticProviderContext
from db.engine import get_session  # noqa: E402
from routes import deps  # noqa: E402
from routes.content.artist import router as artist_router  # noqa: E402
from services.config_manager import ConfigManager  # noqa: E402

from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRegistry  # noqa: E402


class _Discography(MetadataProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name, albums):
        super().__init__(StaticProviderContext())
        self.name = name
        self.label = name
        self._albums = albums

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_artist(self, provider_id, session):
        return Artist(name="Caparezza", provider=self.name, provider_id=provider_id)

    def search_artists(self, query, session, limit=20):
        return [self.get_artist("x", session)]

    def get_artist_albums(self, provider_id, session, limit=50):
        return list(self._albums)


def _client(providers):
    engine = new_engine()
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False, "metadata_provider_priority": [p.name for p in providers]})
    app = FastAPI()
    app.include_router(artist_router)

    def _session():
        with Session(engine) as s:
            yield s

    registry = FakeRegistry(metadata=providers)
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    return TestClient(app)


def test_the_route_paginates_releases_not_editions():
    main = _Discography("pa", [
        _album("Prisoner 709", provider="pa", pid="pa-1", year=2017),
        _album("Prisoner 709 (Deluxe)", provider="pa", pid="pa-2", year=2017),
        _album("Museica", provider="pa", pid="pa-3", year=2014),
        _album("Il sogno eretico", provider="pa", pid="pa-4", year=2011),
    ])
    other = _Discography("pb", [_album("Museica (Deluxe Edition)", provider="pb", pid="pb-1", year=2014)])
    client = _client([main, other])

    first = client.get("/artist/pa/x/albums?page=1&limit=2").json()
    second = client.get("/artist/pa/x/albums?page=2&limit=2").json()

    assert first["total"] == 3 and first["pages"] == 2
    assert [a["title"] for a in first["items"]] == ["Prisoner 709", "Museica"]
    assert [a["title"] for a in second["items"]] == ["Il sogno eretico"]
    museica = first["items"][1]
    assert {(e["provider"], e["provider_id"]) for e in museica["editions"]} == {("pa", "pa-3"), ("pb", "pb-1")}


def test_a_single_is_not_the_album_with_the_same_title():
    assert not match_release(_album("Exuvia"), _album("Exuvia", pid="s", album_type="single")).matched


def test_albums_listed_without_an_artist_are_grouped_as_the_artist_s():
    """Alcuni provider elencano la discografia senza artista: l'host lo
    completa, cosi' le uscite si uniscono (caso reale: due cataloghi)."""
    main = _Discography("pa", [_album("Exuvia", provider="pa", pid="pa-1", year=2021)])
    other = _Discography("pb", [_album("Exuvia", provider="pb", pid="pb-1", year=None, artist="")])

    items = _client([main, other]).get("/artist/pa/x/albums").json()["items"]

    assert len(items) == 1
    assert {e["provider_id"] for e in items[0]["editions"]} == {"pa-1", "pb-1"}


def test_a_yearless_entry_does_not_bridge_two_releases_with_the_same_title():
    # La voce senza anno viene dal provider con priorità più alta, quindi è
    # la prima a formare un gruppo: le due uscite omonime non devono finirci
    # insieme passando da lei.
    yearless = _album("Weezer", provider="pa", year=None)
    blue = _album("Weezer", provider="pb", pid="blue", year=1994)
    green = _album("Weezer", provider="pb", pid="green", year=2001)

    groups = group_releases([yearless, blue, green], ["pa", "pb"])

    assert len(groups) == 2
    assert {tuple(sorted(a.provider_id for a in g.editions)) for g in groups} == {("blue", "pa-Weezer"), ("green",)}


def test_an_edition_does_not_bridge_two_releases_with_the_same_title():
    deluxe = _album("Weezer (Deluxe Edition)", provider="pa", pid="deluxe", year=2004)
    blue = _album("Weezer", provider="pb", pid="blue", year=1994)
    green = _album("Weezer", provider="pb", pid="green", year=2001)

    groups = group_releases([deluxe, blue, green], ["pa", "pb"])

    assert len(groups) == 2


@pytest.mark.parametrize("variant", ["Prisoner 709 (Remixes)", "Prisoner 709 (Acoustic)", "Prisoner 709 - Instrumental"])
def test_remix_acoustic_and_instrumental_albums_stay_separate(variant):
    assert match_release(_album("Prisoner 709"), _album(variant, pid="v")).reason == "release_type_mismatch"


def test_the_same_variant_on_two_providers_still_merges():
    assert match_release(
        _album("Prisoner 709 (Remixes)", provider="pa"), _album("Prisoner 709 - Remixed", provider="pb"),
    ).matched


# ---------------------------------------------------------------------------
# La stessa uscita datata diversamente da due provider (caso reale: "?!" di
# Caparezza, 2000 su un provider e 2010 su un altro, che la data con la
# ristampa digitale: stesse 14 tracce).
# ---------------------------------------------------------------------------


def _counted(title, *, track_count=14, **kwargs):
    album = _album(title, **kwargs)
    album.track_count = track_count
    return album


def test_a_redated_edition_from_another_provider_joins_the_original():
    original = _counted("?!", provider="pa", pid="pa-1", year=2000)
    redated = _counted("?!", provider="pb", pid="pb-1", year=2010)

    groups = group_releases([original, _album("Verità supposte", pid="pa-2", year=2003), redated], ["pa", "pb"])

    assert _titles(groups) == [["Verità supposte"], ["?!", "?!"]]
    assert groups[1].representative.provider_id == "pa-1"
    assert groups[1].year == 2000
    assert serialize_release_group(groups[1])["year"] == 2000
    assert {e["year"] for e in serialize_release_group(groups[1])["editions"]} == {2000, 2010}


def test_the_redated_edition_merges_whatever_the_priority_and_keeps_the_original_year():
    groups = group_releases([
        _counted("?!", provider="pa", pid="pa-1", year=2000),
        _counted("?!", provider="pb", pid="pb-1", year=2010),
    ], ["pb", "pa"])

    assert len(groups) == 1
    assert groups[0].representative.provider_id == "pb-1"
    assert serialize_release_group(groups[0])["year"] == 2000


def test_a_redated_edition_with_an_unknown_track_count_still_merges():
    groups = group_releases([
        _counted("?!", provider="pa", year=2000),
        _counted("?!", provider="pb", year=2010, track_count=None),
    ], ["pa", "pb"])

    assert len(groups) == 1


def test_two_releases_with_the_same_title_on_one_provider_never_merge_across_years():
    """Album omonimi (Weezer 1994, 2001): anche se un altro provider ne
    elenca uno solo, ognuno resta la propria uscita."""
    blue = _counted("Weezer", provider="pa", pid="blue", year=1994, artist="Weezer", track_count=10)
    green = _counted("Weezer", provider="pa", pid="green", year=2001, artist="Weezer", track_count=10)
    red = _counted("Weezer", provider="pb", pid="red", year=2008, artist="Weezer", track_count=10)

    groups = group_releases([blue, green, red], ["pa", "pb"])

    assert sorted(tuple(sorted(a.provider_id for a in g.editions)) for g in groups) == [("blue",), ("green",), ("red",)]


def test_the_same_title_on_both_providers_pairs_by_year_not_by_redating():
    groups = group_releases([
        _counted("Weezer", provider="pa", pid="pa-blue", year=1994, artist="Weezer"),
        _counted("Weezer", provider="pa", pid="pa-green", year=2001, artist="Weezer"),
        _counted("Weezer", provider="pb", pid="pb-blue", year=1994, artist="Weezer"),
        _counted("Weezer", provider="pb", pid="pb-green", year=2001, artist="Weezer"),
    ], ["pa", "pb"])

    assert sorted(tuple(sorted(a.provider_id for a in g.editions)) for g in groups) == [
        ("pa-blue", "pb-blue"), ("pa-green", "pb-green"),
    ]


@pytest.mark.parametrize("other", [
    dict(title="!?"),                                    # stessa punteggiatura, altro ordine
    dict(title="?"),
    dict(title="?!", album_type="compilation"),          # tipo diverso
    dict(title="?!", album_type="single"),
    dict(title="?! (Live)"),                             # variante
    dict(title="?!", artist="Qualcun altro"),            # altro artista
    dict(title="?!", track_count=20),                    # numero di tracce diverso
    dict(title="?!", album_type=None),                   # tipo ignoto: non basta
])
def test_near_misses_of_a_redated_edition_stay_separate(other):
    fields = {"provider": "pb", "pid": "pb-1", "year": 2010, "track_count": 14, **other}
    title = fields.pop("title")
    track_count = fields.pop("track_count")

    groups = group_releases([_counted("?!", provider="pa", pid="pa-1", year=2000),
                             _counted(title, track_count=track_count, **fields)], ["pa", "pb"])

    assert len(groups) == 2


def test_punctuation_only_titles_compare_by_their_punctuation():
    assert match_release(_album("?!"), _album("?!", pid="x")).matched
    assert match_release(_album("?!"), _album("?! (Remastered)", pid="x", year=2020)).matched
    assert match_release(_album("?!"), _album("!?", pid="x")).reason == "title_mismatch"
    assert match_release(_album("?!"), _album("...", pid="x")).reason == "title_mismatch"


# ---------------------------------------------------------------------------
# Ordine della discografia: tipo (ALBUM_TYPES), anno decrescente, titolo.
# ---------------------------------------------------------------------------


def test_releases_are_ordered_by_type_then_newest_first():
    albums = [
        _album("Old Single", pid="s1", year=2004, album_type="single"),
        _album("Museica", pid="a1", year=2014),
        _album("Compilation", pid="c1", year=2020, album_type="compilation"),
        _album("?!", pid="a2", year=2000),
        _album("No Year", pid="a3", year=None),
        _album("Exuvia", pid="a4", year=2021),
        _album("New Single", pid="s2", year=2021, album_type="single"),
        _album("Guest Spot", pid="g1", year=2022, album_type="appears_on"),
        _album("Unknown Type", pid="u1", year=2023, album_type=None),
        _album("EP", pid="e1", year=2010, album_type="ep"),
        _album("Live", pid="l1", year=2012, album_type="live"),
        _album("Il sogno eretico", pid="a5", year=2011),
        _album("Habemus Capa", pid="a6", year=2011),
    ]

    groups = group_releases(albums, ["pa"])

    assert [g.representative.title for g in groups] == [
        "Exuvia", "Museica", "Habemus Capa", "Il sogno eretico", "?!", "No Year",
        "New Single", "Old Single",
        "EP",
        "Compilation",
        "Live",
        "Guest Spot",
        "Unknown Type",
    ]


def test_an_edition_group_sorts_by_its_oldest_edition():
    groups = group_releases([
        _album("Museica", pid="m", year=2014),
        _album("Prisoner 709 (Deluxe)", pid="p2", year=2019),
        _album("Prisoner 709", pid="p1", year=2017),
        _album("Il sogno eretico", pid="s", year=2011),
    ], ["pa"])

    assert [(g.representative.title, g.year) for g in groups] == [
        ("Prisoner 709", 2017), ("Museica", 2014), ("Il sogno eretico", 2011),
    ]
