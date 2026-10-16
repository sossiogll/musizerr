"""
Verifica dell'integrità dei file audio scaricati.

Operazione locale deterministica sul *file* (nessun backend esterno
intercambiabile) → utility di ``core/``, non un provider. Usa ffmpeg/ffprobe
(già presenti nell'immagine). Provider-agnostica: non conosce da dove arriva
il file.

Rileva i due difetti osservati sul campo:
  - contenitore valido ma header/stream non decodificabile
    (es. Ogg valido ma "Invalid Setup header" Vorbis);
  - download troncato (durata reale molto diversa da quella attesa).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
_FFPROBE = shutil.which("ffprobe") or "ffprobe"


@dataclass
class VerifyResult:
    ok: bool
    reason: str | None = None
    duration_s: float | None = None


def probe_duration(path: str, *, timeout: int = 30) -> float | None:
    """Durata del file in secondi via ffprobe, o None se non determinabile."""
    try:
        proc = subprocess.run(
            [_FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, timeout=timeout, text=True,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "{}")
        dur = data.get("format", {}).get("duration")
        return float(dur) if dur is not None else None
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        logger.debug("ffprobe failed on %s: %s", path, exc)
        return None


def verify_audio(
    path: str,
    expected_duration_ms: int | None = None,
    *,
    tolerance_s: int = 5,
    timeout: int = 120,
) -> VerifyResult:
    """
    Verifica che ``path`` sia un file audio integro.

    1) Decodificabilità: ``ffmpeg -v error -i FILE -f null -``. Exit code != 0
       o output su stderr (a livello error) ⇒ corrotto.
    2) Durata: deve esistere ed essere > 0. Se ``expected_duration_ms`` è dato,
       lo scarto dev'essere entro ``tolerance_s`` (altrimenti troncato/incompleto).
    """
    # --- 1) decodificabilità ---
    try:
        proc = subprocess.run(
            [_FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"],
            capture_output=True, timeout=timeout, text=True,
        )
    except subprocess.TimeoutExpired:
        return VerifyResult(False, "ffmpeg timeout during decode")
    except OSError as exc:
        # ffmpeg mancante o errore di sistema: non possiamo verificare.
        logger.error("ffmpeg not runnable for %s: %s", path, exc)
        return VerifyResult(False, f"ffmpeg not runnable: {exc}")

    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0 or stderr:
        return VerifyResult(False, f"decode error: {stderr[:200] or 'exit ' + str(proc.returncode)}")

    # --- 2) durata ---
    dur = probe_duration(path, timeout=min(timeout, 30))
    if dur is None or dur <= 0:
        return VerifyResult(False, "missing or zero duration")

    if expected_duration_ms:
        expected_s = expected_duration_ms / 1000.0
        if abs(dur - expected_s) > tolerance_s:
            return VerifyResult(
                False,
                f"duration mismatch: {dur:.1f}s vs expected {expected_s:.1f}s "
                f"(tolerance {tolerance_s}s)",
                dur,
            )

    return VerifyResult(True, None, dur)
