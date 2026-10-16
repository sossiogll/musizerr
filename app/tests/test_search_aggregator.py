"""
Aggregatore di ricerca: fan-out parallelo, merge per identità, isolamento
errori, budget temporale e — regressione chiave — autenticazione seriale fuori
dai thread (il bug 'session is provisioning a new connection').
"""

from __future__ import annotations

import threading

from core.search_aggregator import SearchAggregator

from tests.conftest import (
    FakeAuthManager,
    FakeMetadataProvider,
    FakeRegistry,
    make_track,
)


def _aggregator(providers, auth=None, priority=None):
    registry = FakeRegistry(metadata=providers)
    return SearchAggregator(
        registry=registry,
        auth_manager=auth or FakeAuthManager(),
        provider_priority=priority or [],
    )


def test_merges_results_from_all_providers():
    p1 = FakeMetadataProvider("p1", tracks=[make_track("A", provider="p1")])
    p2 = FakeMetadataProvider("p2", tracks=[make_track("B", provider="p2")])
    agg = _aggregator([p1, p2])

    titles = {t.title for t in agg.search_tracks("q")}
    assert titles == {"A", "B"}


def test_provider_failure_is_isolated():
    ok = FakeMetadataProvider("ok", tracks=[make_track("A", provider="ok")])
    boom = FakeMetadataProvider("boom", fail=True)
    agg = _aggregator([ok, boom])

    results = agg.search_tracks("q")
    assert [t.title for t in results] == ["A"]  # il crash di 'boom' non propaga


def test_shared_isrc_is_deduplicated_across_providers():
    p1 = FakeMetadataProvider("p1", tracks=[make_track("Song", isrc="ISRC1", provider="p1")])
    p2 = FakeMetadataProvider("p2", tracks=[make_track("Song (Remaster)", isrc="ISRC1", provider="p2")])
    agg = _aggregator([p1, p2])

    results = agg.search_tracks("q")
    assert len(results) == 1  # stesso ISRC → un solo item unito


def test_provider_filter_limits_fan_out():
    p1 = FakeMetadataProvider("p1", tracks=[make_track("A", provider="p1")])
    p2 = FakeMetadataProvider("p2", tracks=[make_track("B", provider="p2")])
    agg = _aggregator([p1, p2])

    results = agg.search_tracks("q", provider_filter=["p1"])
    assert [t.title for t in results] == ["A"]
    assert p2.search_threads == []  # p2 mai interrogato


def test_authentication_happens_serially_on_main_thread():
    # Regressione: authenticate() tocca una Session DB condivisa; DEVE avvenire
    # nel thread chiamante, non dentro i worker del pool (altrimenti race).
    auth = FakeAuthManager()
    p1 = FakeMetadataProvider("p1", tracks=[make_track("A", provider="p1")])
    p2 = FakeMetadataProvider("p2", tracks=[make_track("B", provider="p2")])
    agg = _aggregator([p1, p2], auth=auth)

    main = threading.get_ident()
    agg.search_tracks("q")

    # tutte le auth sul thread principale...
    assert auth.auth_threads and all(tid == main for tid in auth.auth_threads)
    # ...mentre le ricerche girano nei thread del pool (parallelismo reale).
    ran_off_main = [tid for p in (p1, p2) for tid in p.search_threads if tid != main]
    assert ran_off_main, "le ricerche dovrebbero girare in thread separati"


def test_slow_provider_dropped_by_budget():
    # Budget configurabile passato al costruttore (dal catalogo, via deps).
    fast = FakeMetadataProvider("fast", tracks=[make_track("A", provider="fast")])
    slow = FakeMetadataProvider("slow", tracks=[make_track("B", provider="slow")], delay=2.0)
    agg = SearchAggregator(
        registry=FakeRegistry(metadata=[fast, slow]),
        auth_manager=FakeAuthManager(),
        search_budget_seconds=0.2,
    )

    results = agg.search_tracks("q")
    assert [t.title for t in results] == ["A"]  # 'slow' oltre budget → scartato


def test_empty_registry_returns_empty():
    agg = _aggregator([])
    assert agg.search_tracks("q") == []
