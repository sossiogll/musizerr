"""
Copre un bug reale trovato leggendo il mediaInfo REALE di un download
provider delegato contro gli 11 download fermi in 'dispatched': il campo
arriva come stringa con unità (es. ``"320 kbps"``, ``"44.1kHz"``), mentre il
vecchio codice faceva ``int()`` diretto — che solleva ``ValueError`` su
qualunque stringa non puramente numerica, quindi bitrate/sample-rate
finivano SEMPRE a 0 (sconosciuto), anche quando il dato era disponibile.

``core.audio_quality.parse_bitrate``/``parse_sample_rate`` sono le utility
GENERICHE (nessun nome di provider, nessun nome di campo specifico di
un'API — quello resta nel provider che lo legge) che interpretano un
valore già numerico o una stringa con unità leggibile, senza mai inventare
un valore da una stringa non interpretabile.

Tabellare per copertura esplicita di ogni forma osservata/plausibile.
"""

from __future__ import annotations

import pytest

from musizeer_sdk.text import parse_bitrate, parse_sample_rate


@pytest.mark.parametrize(
    "value, expected",
    [
        # Forma reale misurata: "320 kbps" (con spazio, minuscolo).
        ("320 kbps", 320_000),
        ("320kbps", 320_000),
        ("320 KBPS", 320_000),  # case-insensitive
        ("1411 kbps", 1_411_000),
        ("1.5 mbps", 1_500_000),
        ("128", 128),           # nessuna unità: già in bit/s
        (320_000, 320_000),     # già numerico: passthrough
        (320_000.0, 320_000),
        (0, 0),
        (None, 0),
        ("", 0),                # forma reale: audioBits vuoto per alcuni codec
        ("   ", 0),
        ("not a number", 0),    # non interpretabile → sconosciuto, mai inventato
        ("320 gbps", 0),        # unità sconosciuta → sconosciuto, mai un valore a caso
        (True, 0),               # bool è sottoclasse di int: mai un bitrate plausibile
    ],
)
def test_parse_bitrate(value, expected):
    assert parse_bitrate(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        # Forma reale misurata: "44.1kHz" (senza spazio).
        ("44.1kHz", 44_100),
        ("44.1 kHz", 44_100),
        ("44100Hz", 44_100),
        ("44100 hz", 44_100),
        ("48kHz", 48_000),
        ("48000", 48_000),      # nessuna unità: già in Hz
        (44_100, 44_100),       # già numerico: passthrough
        (44_100.0, 44_100),
        (0, 0),
        (None, 0),
        ("", 0),                 # forma reale: campo assente/vuoto
        ("unknown", 0),
        ("44.1 ghz", 0),         # unità sconosciuta → sconosciuto
    ],
)
def test_parse_sample_rate(value, expected):
    assert parse_sample_rate(value) == expected
