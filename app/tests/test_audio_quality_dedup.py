"""
Copre un bug reale trovato leggendo il codice contro gli 11 download reali
fermi in 'dispatched': la vecchia policy di dedup confrontava
``FileRef.quality_key()`` come tupla ``(lossless_rank, bitrate, sample_rate)``
dove un bitrate/sample_rate NON riportato dal provider diventa 0 — quindi tra
due file lossy, quello con bitrate sconosciuto perdeva SEMPRE il confronto ed
era cancellato, anche quando in realtà poteva essere il file migliore.
``core.audio_quality.decide_dedup`` è la policy pura corretta: rifiuta di
decidere (``UNDECIDED``) ogni volta che il campo che DECIDEREBBE il confronto
è sconosciuto su almeno un lato, a parità di classe lossless. Una differenza
di classe lossless resta sempre decisiva di per sé (è un booleano, non può
essere "sconosciuta").

Tabellare per copertura esplicita di ogni combinazione richiesta.
"""

from __future__ import annotations

import pytest

from core.audio_quality import KEEP_EXISTING, KEEP_INCOMING, UNDECIDED, decide_dedup


def _decide(
    *,
    inc_lossless=False, inc_bitrate=0, inc_sr=0,
    exi_lossless=False, exi_bitrate=0, exi_sr=0,
    prefer_lossless=True,
):
    return decide_dedup(
        incoming_lossless=inc_lossless, incoming_bitrate=inc_bitrate, incoming_sample_rate=inc_sr,
        existing_lossless=exi_lossless, existing_bitrate=exi_bitrate, existing_sample_rate=exi_sr,
        prefer_lossless=prefer_lossless,
    )


# ---------------------------------------------------------------------------
# Lossless vs lossy — decisivo di per sé, in entrambe le direzioni
# ---------------------------------------------------------------------------

def test_lossless_incoming_beats_lossy_existing_regardless_of_bitrate():
    # L'existing (lossy) ha un bitrate NOTO e alto: non basta, la classe
    # lossless è decisiva a prescindere.
    decision = _decide(inc_lossless=True, inc_bitrate=0, exi_lossless=False, exi_bitrate=320_000)
    assert decision.action == KEEP_INCOMING
    assert "lossless" in decision.reason


def test_lossless_existing_beats_lossy_incoming_regardless_of_bitrate():
    decision = _decide(inc_lossless=False, inc_bitrate=320_000, exi_lossless=True, exi_bitrate=0)
    assert decision.action == KEEP_EXISTING
    assert "lossless" in decision.reason


# ---------------------------------------------------------------------------
# Due lossy, bitrate noto su entrambi i lati → decisivo normalmente
# ---------------------------------------------------------------------------

def test_two_lossy_known_bitrates_incoming_higher():
    decision = _decide(inc_lossless=False, inc_bitrate=320_000, exi_lossless=False, exi_bitrate=128_000)
    assert decision.action == KEEP_INCOMING
    assert decision.reason == "higher bitrate"


def test_two_lossy_known_bitrates_existing_higher():
    decision = _decide(inc_lossless=False, inc_bitrate=128_000, exi_lossless=False, exi_bitrate=320_000)
    assert decision.action == KEEP_EXISTING
    assert decision.reason == "higher bitrate"


# ---------------------------------------------------------------------------
# IL BUG: due lossy, bitrate sconosciuto (0) su UN lato → mai deciso
# ---------------------------------------------------------------------------

def test_two_lossy_incoming_bitrate_unknown_stays_undecided():
    # Prima del fix: bitrate 0 < 320000 → l'incoming (bitrate sconosciuto)
    # perdeva sempre, anche se magari era il file migliore.
    decision = _decide(inc_lossless=False, inc_bitrate=0, exi_lossless=False, exi_bitrate=320_000)
    assert decision.action == UNDECIDED


def test_two_lossy_existing_bitrate_unknown_stays_undecided():
    decision = _decide(inc_lossless=False, inc_bitrate=320_000, exi_lossless=False, exi_bitrate=0)
    assert decision.action == UNDECIDED


def test_two_lossy_both_bitrates_unknown_stays_undecided():
    # 0 == 0 non è un pareggio reale: è assenza di informazione sui due lati.
    decision = _decide(inc_lossless=False, inc_bitrate=0, exi_lossless=False, exi_bitrate=0)
    assert decision.action == UNDECIDED


def test_two_lossless_bitrate_unknown_on_one_side_stays_undecided():
    # Stessa regola vale anche tra due lossless (es. due FLAC, uno senza
    # bitrate riportato dal delegato).
    decision = _decide(inc_lossless=True, inc_bitrate=0, exi_lossless=True, exi_bitrate=900_000)
    assert decision.action == UNDECIDED


def test_bitrate_tied_sample_rate_unknown_on_one_side_stays_undecided():
    # Il bitrate NON decide (identico e noto): il sample rate diventerebbe il
    # campo decisivo, ma è sconosciuto su un lato → non deciso.
    decision = _decide(
        inc_lossless=True, inc_bitrate=900_000, inc_sr=0,
        exi_lossless=True, exi_bitrate=900_000, exi_sr=44_100,
    )
    assert decision.action == UNDECIDED


def test_bitrate_differs_sample_rate_unknown_is_irrelevant():
    # Il bitrate DECIDE già (differisce e noto): il sample rate sconosciuto
    # non deve impedire la decisione, perché non è mai consultato.
    decision = _decide(
        inc_lossless=True, inc_bitrate=900_000, inc_sr=0,
        exi_lossless=True, exi_bitrate=700_000, exi_sr=0,
    )
    assert decision.action == KEEP_INCOMING
    assert decision.reason == "higher bitrate"


# ---------------------------------------------------------------------------
# Pareggio esatto → tieni l'esistente, come oggi
# ---------------------------------------------------------------------------

def test_exact_tie_keeps_existing_copy():
    decision = _decide(
        inc_lossless=True, inc_bitrate=900_000, inc_sr=44_100,
        exi_lossless=True, exi_bitrate=900_000, exi_sr=44_100,
    )
    assert decision.action == KEEP_EXISTING
    assert decision.reason == "exact tie"


def test_exact_tie_between_two_lossy_with_known_equal_fields_keeps_existing():
    decision = _decide(
        inc_lossless=False, inc_bitrate=320_000, inc_sr=44_100,
        exi_lossless=False, exi_bitrate=320_000, exi_sr=44_100,
    )
    assert decision.action == KEEP_EXISTING
    assert decision.reason == "exact tie"


# ---------------------------------------------------------------------------
# prefer_lossless=False — la classe lossless non conta più
# ---------------------------------------------------------------------------

def test_prefer_lossless_off_ignores_lossless_class_and_falls_back_to_bitrate():
    # Con prefer_lossless=False i due "rank" collassano entrambi a 0: il
    # bitrate (noto su entrambi i lati) diventa decisivo anche tra un
    # lossless e un lossy.
    decision = _decide(
        inc_lossless=True, inc_bitrate=128_000,
        exi_lossless=False, exi_bitrate=320_000,
        prefer_lossless=False,
    )
    assert decision.action == KEEP_EXISTING
    assert decision.reason == "higher bitrate"


def test_prefer_lossless_off_still_refuses_on_unknown_bitrate():
    decision = _decide(
        inc_lossless=True, inc_bitrate=0,
        exi_lossless=False, exi_bitrate=320_000,
        prefer_lossless=False,
    )
    assert decision.action == UNDECIDED


@pytest.mark.parametrize(
    "prefer_lossless",
    [True, False],
)
def test_identical_lossless_flag_never_alone_decides_when_ranks_equal(prefer_lossless):
    # Con prefer_lossless=False ENTRAMBI i rank sono 0 indipendentemente dal
    # flag "lossless" reale — la classe non è più decisiva, il bitrate lo è.
    decision = _decide(
        inc_lossless=True, inc_bitrate=100_000,
        exi_lossless=True, exi_bitrate=200_000,
        prefer_lossless=prefer_lossless,
    )
    assert decision.action == KEEP_EXISTING
    assert decision.reason == "higher bitrate"
