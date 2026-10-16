"""
Regole di convergenza (pure) della riconciliazione dei like verso il sync provider.

Isolate qui perché sono la parte più delicata e vanno testate senza toccare
celery/DB/provider. Provider-agnostico: nessun riferimento a provider concreti.
"""

from __future__ import annotations

# Azioni possibili a ogni giro del reconciler.
LIKE = "like"                     # traccia trovata nel sync → applica il like e chiudi
SKIP_NO_ACCOUNT = "skip_no_account"  # utente senza account personale → non fare nulla
GIVE_UP = "give_up"               # oltre il max-age e mai comparsa → abbandona
RETRY = "retry"                   # non ancora indicizzata → ri-tenta più tardi


def decide_like_action(
    *,
    has_own_account: bool,
    sync_id: str | None,
    age_seconds: float | None,
    max_age_seconds: float,
) -> str:
    """
    Decide cosa fare per un marcatore di like, dato lo stato osservato.

    - Nessun account personale → SKIP (il like è un dato personale, mai
      sull'account di sistema condiviso).
    - Traccia risolta nel sync provider → LIKE.
    - Non risolta e oltre il max-age → GIVE_UP (lo scan non l'ha mai indicizzata).
    - Non risolta ma ancora nei tempi → RETRY (lag di indicizzazione, converge dopo).
    """
    if not has_own_account:
        return SKIP_NO_ACCOUNT
    if sync_id:
        return LIKE
    if age_seconds is not None and age_seconds > max_age_seconds:
        return GIVE_UP
    return RETRY


# Un like che aspetta l'indicizzazione si ritenta con backoff esponenziale
# (``core.backoff.retry_delay``): dall'intervallo configurato, raddoppiando,
# fino a un'ora.
MAX_RETRY_INTERVAL_SECONDS = 3600
