"""
Ranking di qualità audio, generico e provider-agnostico.

Policy pura per il dedup: a parità di traccia si tiene la copia di qualità
maggiore. Regola: lossless batte sempre lossy; a parità di tipo vince il
bitrate più alto, poi il sample rate.

Due modalità:
  - da FILE (``probe_quality``): estrae codec/bitrate/sample-rate via ffprobe;
  - da METADATA (``quality_tuple``): quando i valori arrivano già pronti
    (es. mediaInfo dell'API di un download provider delegato) senza toccare i byte.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

from musizerr_sdk.text import LOSSLESS_CODECS

logger = logging.getLogger(__name__)

_FFPROBE = shutil.which("ffprobe") or "ffprobe"


@dataclass(frozen=True)
class QualityScore:
    lossless: bool
    bitrate: int      # bit/s, 0 se sconosciuto
    sample_rate: int  # Hz, 0 se sconosciuto
    codec: str = ""

    def as_tuple(self, prefer_lossless: bool = True) -> tuple[int, int, int]:
        """Tupla ordinabile: più grande = migliore."""
        lossless_rank = 1 if (self.lossless and prefer_lossless) else 0
        return (lossless_rank, self.bitrate, self.sample_rate)


def quality_tuple(
    *, lossless: bool, bitrate: int = 0, sample_rate: int = 0,
    prefer_lossless: bool = True,
) -> tuple[int, int, int]:
    """Ranking da valori già noti (senza file). Più grande = migliore."""
    return QualityScore(lossless, bitrate, sample_rate).as_tuple(prefer_lossless)


def probe_quality(path: str, *, timeout: int = 30) -> QualityScore | None:
    """Estrae la qualità dal file via ffprobe, o None se non determinabile."""
    try:
        proc = subprocess.run(
            [_FFPROBE, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,bit_rate,sample_rate",
             "-show_entries", "format=bit_rate",
             "-of", "json", path],
            capture_output=True, timeout=timeout, text=True,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or [{}]
        st = streams[0]
        codec = (st.get("codec_name") or "").lower()
        bitrate = _to_int(st.get("bit_rate")) or _to_int(data.get("format", {}).get("bit_rate"))
        sample_rate = _to_int(st.get("sample_rate"))
        return QualityScore(
            lossless=codec in LOSSLESS_CODECS,
            bitrate=bitrate,
            sample_rate=sample_rate,
            codec=codec,
        )
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        logger.debug("ffprobe quality failed on %s: %s", path, exc)
        return None


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------
# Dedup policy — decisione pura "quale copia tenere", mai su una scommessa
# ------------------------------------------------------------------
#
# ATTENZIONE (contesto): questa policy gira SOLO quando un download provider
# riporta conflitti FILE-LEVEL (``DelegationStatus.conflicts`` popolato con
# ``FileRef`` concreti — vedi musizerr_sdk.delegation). Nessun provider album-level
# lo fa oggi (verificato sui provider di prima parte): il suo poll_delegation riporta un conflitto di import
# solo come messaggio di testo, mai come ``FileRef`` — la sua stessa quality
# profile ha già scelto la copia migliore a monte. Questa funzione resta
# quindi inerte in produzione finché un provider futuro non fornisce
# conflitti a livello di file; i duplicati CROSS-sistema sono un problema
# distinto e deliberatamente rimandato (vedi ADR-0001, D4). Non estenderla
# per fare rilevamento di conflitti per quel provider: non è lo scopo.

KEEP_INCOMING = "keep_incoming"
KEEP_EXISTING = "keep_existing"
UNDECIDED = "undecided"


@dataclass(frozen=True)
class DedupDecision:
    """Esito puro della policy di dedup: quale copia tenere, e perché."""
    action: str    # KEEP_INCOMING | KEEP_EXISTING | UNDECIDED
    reason: str


def decide_dedup(
    *,
    incoming_lossless: bool, incoming_bitrate: int, incoming_sample_rate: int,
    existing_lossless: bool, existing_bitrate: int, existing_sample_rate: int,
    prefer_lossless: bool = True,
) -> DedupDecision:
    """
    Decide quale delle due copie di UNA STESSA traccia tenere, senza mai
    inventare un esito da un valore mancante. ``bitrate``/``sample_rate`` a
    0 significa "il provider non l'ha riportato" (vedi ``QualityScore``),
    non "zero reale" — un valore del genere non può mai essere decisivo.

    Ordine dei campi (lossless class → bitrate → sample rate), stesso
    dell'ordinamento storico di ``QualityScore.as_tuple``, ma con un rifiuto
    esplicito (``UNDECIDED``) ogni volta che il campo che DECIDEREBBE il
    confronto è sconosciuto su almeno un lato. Una differenza di classe
    lossless è sempre decisiva di per sé (è un booleano, non può essere
    "sconosciuta"). A parità totale su campi noti: tieni la copia esistente
    (comportamento storico).
    """
    inc_rank = 1 if (incoming_lossless and prefer_lossless) else 0
    exi_rank = 1 if (existing_lossless and prefer_lossless) else 0
    if inc_rank != exi_rank:
        action = KEEP_INCOMING if inc_rank > exi_rank else KEEP_EXISTING
        return DedupDecision(action, "different lossless class")

    # Stessa classe lossless: il bitrate è il campo decisivo successivo.
    if incoming_bitrate != existing_bitrate:
        if incoming_bitrate == 0 or existing_bitrate == 0:
            return DedupDecision(UNDECIDED, "bitrate unknown on one side, same lossless class")
        action = KEEP_INCOMING if incoming_bitrate > existing_bitrate else KEEP_EXISTING
        return DedupDecision(action, "higher bitrate")
    if incoming_bitrate == 0:
        # Bitrate "paritario" solo perché ENTRAMBI sconosciuti — non è un
        # pareggio reale, è assenza di informazione su entrambi i lati.
        return DedupDecision(UNDECIDED, "bitrate unknown on both sides, same lossless class")

    # Bitrate identico e noto: il sample rate è il prossimo campo decisivo.
    if incoming_sample_rate != existing_sample_rate:
        if incoming_sample_rate == 0 or existing_sample_rate == 0:
            return DedupDecision(UNDECIDED, "sample rate unknown on one side, bitrate tied")
        action = KEEP_INCOMING if incoming_sample_rate > existing_sample_rate else KEEP_EXISTING
        return DedupDecision(action, "higher sample rate")
    if incoming_sample_rate == 0:
        return DedupDecision(UNDECIDED, "sample rate unknown on both sides, bitrate tied")

    # Pareggio esatto su tutti i campi noti: tieni la copia esistente.
    return DedupDecision(KEEP_EXISTING, "exact tie")
