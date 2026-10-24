"""
Pure text and value helpers a provider needs, standard library only: the
host uses these very functions for its own comparisons, so a provider and the
host normalize a title the same way. Matching policy (what makes two tracks
the same recording) stays in the host.
"""
from __future__ import annotations

import re
import unicodedata

# Codec senza perdita noti.
LOSSLESS_CODECS = frozenset({
    "flac", "alac", "wav", "pcm_s16le", "pcm_s24le", "pcm_s32le",
    "ape", "wavpack", "tak", "tta", "mlp", "truehd",
})


def parse_year(date_str: str | None) -> int | None:
    """Extract year from date string (YYYY or YYYY-MM-DD format)."""
    if date_str and len(date_str) >= 4:
        try:
            return int(date_str[:4])
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------
# Parsing generico di valori con unità (bitrate/sample-rate) — provider-
# agnostico: legge SOLO numero+unità, mai un nome di campo specifico di
# un'API (quello resta nel provider, che sa come si chiama il proprio campo).
# ------------------------------------------------------------------
#
# Alcuni sistemi esterni riportano bitrate/sample-rate già come numero
# (bit/s o Hz), altri come stringa leggibile con unità (es. "320 kbps",
# "44.1kHz") — dipende da come il sistema esterno stesso ha estratto il
# valore (es. mediaInfo/ffprobe con formattazione applicata). Un valore
# vuoto o non interpretabile resta sconosciuto (0), MAI inventato.

_UNIT_VALUE_RE = re.compile(r"^([\d.]+)\s*([a-z]*)$")

_BITRATE_UNIT_MULTIPLIERS: dict[str, int] = {
    "": 1, "bps": 1, "kbps": 1_000, "mbps": 1_000_000,
}
_SAMPLE_RATE_UNIT_MULTIPLIERS: dict[str, int] = {
    "": 1, "hz": 1, "khz": 1_000, "mhz": 1_000_000,
}


def parse_bitrate(value) -> int:
    """
    Bitrate in bit/s da un valore numerico già pronto o da una stringa con
    unità leggibile (es. ``"320 kbps"`` → 320000). Stringa vuota o non
    interpretabile (es. ``""``) → 0 (sconosciuto), mai un valore inventato.
    """
    return _parse_unit_value(value, _BITRATE_UNIT_MULTIPLIERS)


def parse_sample_rate(value) -> int:
    """
    Sample rate in Hz da un valore numerico già pronto o da una stringa con
    unità leggibile (es. ``"44.1kHz"`` → 44100). Stringa vuota o non
    interpretabile → 0 (sconosciuto), mai un valore inventato.
    """
    return _parse_unit_value(value, _SAMPLE_RATE_UNIT_MULTIPLIERS)


def _parse_unit_value(value, unit_multipliers: dict[str, int]) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):  # bool è sottoclasse di int: mai un valore plausibile qui
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if not text:
        return 0
    match = _UNIT_VALUE_RE.match(text)
    if not match:
        return 0
    number_part, unit_part = match.groups()
    try:
        number = float(number_part)
    except ValueError:
        return 0
    multiplier = unit_multipliers.get(unit_part)
    if multiplier is None:
        return 0
    return int(round(number * multiplier))


# Omoglifi: lettere non-latine visivamente identiche a lettere latine, usate nei
# nomi "stilizzati" (es. la Я di "KoЯn", cirillica ma che sta per R). Mappandole
# al latino PRIMA del fold ASCII, "KoЯn" → "korn" invece di "kon", così il match
# universale le aggancia. Solo per il confronto fuzzy, mai per la visualizzazione.
_HOMOGLYPHS = str.maketrans({
    # Cirillico → Latino
    "А": "A", "а": "a", "В": "B", "Е": "E", "е": "e", "Ѕ": "S", "І": "I",
    "Ј": "J", "К": "K", "М": "M", "Н": "H", "О": "O", "о": "o", "Р": "P",
    "р": "p", "С": "C", "с": "c", "Т": "T", "у": "y", "Х": "X", "х": "x",
    "Я": "R", "я": "r",
    # Greco → Latino
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "ο": "o", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
})


# The characters that are a dash or a hyphen: U+002D, the U+2010..U+2015 block
# (hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar),
# the minus sign U+2212 and the rarer hyphen-like marks of other scripts. One
# set, so every place that reads a dash (``normalize``, the tail after " - ",
# the separators of a video title) reads the same ones: "1987-2012" and
# "1987–2012" are the same title, "Song - Live" and "Song – Live" the same
# version tail. (The compatibility forms, like U+FE58 or U+FF0D, are folded by
# NFKD before ``normalize`` looks.)
DASH_CHARACTERS = "\\-\u2010-\u2015\u2043\u058a\u05be\u1806\u2212\u2e1a\u2e3a\u2e3b\u2e40"
_DASH_CLASS = f"[{DASH_CHARACTERS}]"
_DASH_OR_QUOTE_RE = re.compile(f"[{DASH_CHARACTERS}\u2018\u2019\u201a\u201b\u2032\u2035]")


def normalize(text: str) -> str:
    """Rimuove punteggiatura, lowercase, strip — per confronto fuzzy. Ogni
    trattino (``DASH_CHARACTERS``) vale come ogni altro: diventa uno spazio."""
    text = unicodedata.normalize("NFKD", text)
    text = text.translate(_HOMOGLYPHS)
    text = _DASH_OR_QUOTE_RE.sub(" ", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.lower().strip()


# Parentesi/quadre (es. "(feat. X)", "(Remastered)", "[Bonus Track]") e coda
# dopo un trattino spaziato (es. "- 2012 Remaster", "- Rap Version", "- Live"),
# qualunque trattino sia (un en dash vale come un trattino: ``DASH_CHARACTERS``).
PARENS_RE = re.compile(r"\s*[\(\[].*?[\)\]]")
DASH_TAIL_RE = re.compile(rf"\s+{_DASH_CLASS}\s+(.*)$")


# Words of the decoration a video title carries around the song ("(Official
# Video)", "[HD]", "- Lyrics", "(Video Ufficiale)"): never part of the song and
# never a version of it. A tail is noise when ALL its words are noise words and
# at least one is a strong one (a lone "Music" or "Con" says nothing).
_NOISE_STRONG = frozenset({
    "official", "officiel", "oficial", "ufficiale", "video", "videoclip", "audio", "lyric", "lyrics", "lyrical",
    "visualizer", "visualiser", "testo", "testi", "hd", "hq", "uhd", "4k", "8k", "1080p", "720p", "2160p", "mv",
})
_NOISE_WEAK = frozenset({"music", "with", "con", "the", "and", "full", "clip", "in"})
_NOISE_WORDS = _NOISE_STRONG | _NOISE_WEAK
_BRACKETS_RE = re.compile(r"\s*[\(\[\{]([^()\[\]{}]*)[\)\]\}]")
_NOISE_SEPARATOR_RE = re.compile(rf"\s+(?:{_DASH_CLASS}|[|•])\s+|\s*\|\s*")
# A noise phrase glued to the end of a title with no punctuation: only the
# multi-word ones that no song title ends with.
_NOISE_BARE_TAIL_RE = re.compile(
    r"(?<=[\w)\]])\s+(?:official\s+(?:music\s+)?video|official\s+audio|official\s+lyrics?\s+video|(?:lyrics?|lyrical)\s+video"
    r"|video\s+ufficiale|audio\s+ufficiale)$",
    re.I,
)


def is_video_noise(text: str) -> bool:
    """Whether ``text`` (a bracket's content, the tail after a separator) is
    only the decoration of a video title: "Official Music Video", "HD",
    "Lyrics", "Video Ufficiale", "Testo"."""
    words = normalize(text).split()
    return bool(words) and all(word in _NOISE_WORDS for word in words) and any(word in _NOISE_STRONG for word in words)


def strip_video_noise(title: str) -> str:
    """``title`` without the decoration of a video title: the bracketed
    ``(Official Video)`` / ``[HD]`` groups, a tail after " - " or "|" that is
    only noise, a bare " Official Video" at the end. A real version tail
    ("(Live)", "- Radio Edit") stays, and so does a title that would be left
    empty."""
    cleaned = title
    while True:
        before = cleaned
        cleaned = _BRACKETS_RE.sub(lambda m: "" if is_video_noise(m.group(1)) else m.group(0), cleaned)
        separators = list(_NOISE_SEPARATOR_RE.finditer(cleaned))
        if separators and is_video_noise(cleaned[separators[-1].end():]):
            cleaned = cleaned[:separators[-1].start()]
        cleaned = _NOISE_BARE_TAIL_RE.sub("", cleaned).strip()
        if cleaned == before:
            break
    return cleaned or title


def strip_version_suffix(title: str) -> str:
    """
    Rimuove le "code" di versione da un titolo per il confronto con la libreria:
    parentetiche e tutto ciò che segue un " - " (remaster, remix, live, feat...),
    più la decorazione di un titolo di video (``strip_video_noise``).
    Provider-agnostico. Se lo strip svuota il titolo, ritorna l'originale.
    """
    core = DASH_TAIL_RE.sub("", PARENS_RE.sub("", strip_video_noise(title))).strip()
    return core or title

