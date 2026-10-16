"""
Tracce simili (``MetadataProvider.find_similar_tracks``): i provider il cui
servizio ha un endpoint di raccomandazione lo implementano; la rotta
``/track/{provider}/{id}/related`` resta generica e le aggiunge dopo le
varianti della stessa traccia. Come un provider reale chiama il suo servizio
si prova nel suo repository; qui la rotta dell'host, con provider finti.
"""
from __future__ import annotations

from musizerr_sdk.auth import AuthFlow, AuthSession

from tests.test_related_content import SOURCE, _client, _Fake, _t

SESSION = AuthSession(flow=AuthFlow.NONE)


# ---------------------------------------------------------------------------
# Rotta generica: varianti prima, poi simili
# ---------------------------------------------------------------------------


class _Similar(_Fake):
    def __init__(self, name, *, similar=(), fail_similar=False, **kwargs):
        super().__init__(name, **kwargs)
        self._similar, self._fail_similar = list(similar), fail_similar

    def find_similar_tracks(self, track, session, limit=20):
        if self._fail_similar:
            raise RuntimeError("503")
        return self._similar[:limit]


def test_related_lists_variants_then_similar_tracks_in_priority_order():
    pa = _Similar("pa", track=SOURCE,
                  related=[_t("pa", "pa-live", title="Larsen (Live)", album="Live 2018", year=2018)],
                  similar=[_t("pa", "pa-sim", title="Confusianesimo")])
    pb = _Similar("pb", similar=[
        _t("pb", "pb-sim", title="Ti fa stare bene"),
        _t("pb", "pb-same", title="Larsen"),                   # la traccia stessa, da un altro provider
        _t("pb", "pb-conf", title="Confusianesimo"),           # anche da pa: vince pb, prioritario
    ])

    items = _client([pa, pb], ["pb", "pa"]).get("/track/pa/src/related").json()["items"]

    # Le varianti (pa-live) vengono prima di ogni simile, anche di pb.
    assert [i["provider_id"] for i in items] == ["pa-live", "pb-sim", "pb-conf"]


def test_similar_tracks_fill_up_to_the_limit_and_a_failing_provider_is_skipped():
    pa = _Similar("pa", track=SOURCE, similar=[_t("pa", f"s{i}", title=f"Song {i}") for i in range(5)])
    pb = _Similar("pb", fail_similar=True)

    resp = _client([pa, pb], ["pb", "pa"]).get("/track/pa/src/related?limit=3")

    assert resp.status_code == 200
    assert [i["provider_id"] for i in resp.json()["items"]] == ["s0", "s1", "s2"]


def test_the_contract_default_is_no_similar_tracks():
    assert _Fake("pa").find_similar_tracks(SOURCE, SESSION) == []
