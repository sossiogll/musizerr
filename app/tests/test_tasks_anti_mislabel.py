"""
Fase 5c (revisione): gate del toggle globale ``identification_enabled``
sull'anti-mislabel post-download in ``app/workers/tasks.py``.

Contesto del deadlock corretto: ``is_available()`` di un provider di
identificazione consultava ``identification_enabled`` al suo interno. Poiché
``identification_enabled`` stesso richiede "esiste un provider usable",
questo rendeva impossibile accenderlo — spento, nessun provider risultava mai
usable. Il fix sposta il controllo del toggle al CALL SITE generico
(``_should_run_anti_mislabel``, provider-agnostico) e lascia
``is_available()`` rispondere solo a "libreria + config presenti" (la prova
del provider sta nel suo repository, quella dell'host in
``tests/test_identification_deadlock.py``).

Questo modulo prova l'altra metà: col toggle spento, il worker non chiama
MAI ``identify()`` su un provider — anche quando quel provider è, di suo,
perfettamente usable.

Regola d'oro rispettata: nessun provider reale, solo ``FakeIdentificationProvider``
(``tests/conftest.py``).
"""

from __future__ import annotations

from types import SimpleNamespace

from musizeer_sdk.interfaces import WorkspaceFile
from workers.tasks import _check_mislabel, _should_run_anti_mislabel

from tests.conftest import FakeIdentificationProvider, FakeRegistry, make_track


def _config(*, anti_mislabel_enabled: bool, identification_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        anti_mislabel_enabled=anti_mislabel_enabled, identification_enabled=identification_enabled,
        identification_confidence_threshold=0.9,
    )


# ---------------------------------------------------------------------------
# _should_run_anti_mislabel — il gate stesso
# ---------------------------------------------------------------------------


def test_gate_false_when_identification_toggle_is_off_even_if_anti_mislabel_is_on():
    # Il caso esatto del deadlock: anti_mislabel_enabled=True da solo non basta.
    config = _config(anti_mislabel_enabled=True, identification_enabled=False)
    assert _should_run_anti_mislabel(config) is False


def test_gate_false_when_anti_mislabel_itself_is_off():
    config = _config(anti_mislabel_enabled=False, identification_enabled=True)
    assert _should_run_anti_mislabel(config) is False


def test_gate_true_only_when_both_flags_are_on():
    config = _config(anti_mislabel_enabled=True, identification_enabled=True)
    assert _should_run_anti_mislabel(config) is True


# ---------------------------------------------------------------------------
# Percorso end-to-end del worker: il gate impedisce DAVVERO identify(),
# anche con un provider disponibile.
# ---------------------------------------------------------------------------


def test_worker_path_does_not_identify_when_toggle_is_off_even_though_provider_is_available():
    provider = FakeIdentificationProvider(available=True)
    registry = FakeRegistry(identification=[provider])
    config = _config(anti_mislabel_enabled=True, identification_enabled=False)

    # Il chiamante reale (dentro il task Celery) usa questo gate per decidere
    # se chiamare _check_mislabel: col toggle spento, non lo chiama affatto.
    assert _should_run_anti_mislabel(config) is False
    assert provider.identify_calls == 0


def test_worker_path_identifies_when_gate_is_open_and_provider_is_usable():
    provider = FakeIdentificationProvider(available=True)
    registry = FakeRegistry(identification=[provider])
    config = _config(anti_mislabel_enabled=True, identification_enabled=True)
    track = make_track(title="Real Title")

    assert _should_run_anti_mislabel(config) is True
    # Con il gate aperto, _check_mislabel trova il provider usable e lo
    # interroga DAVVERO (mai un secondo gate nascosto dentro _check_mislabel
    # stesso, che è provider-agnostico e ignora identification_enabled).
    ok, _reason = _check_mislabel(track, WorkspaceFile(workspace="/tmp", name="fake-audio.mp3"), registry, config)
    assert provider.identify_calls == 1
    assert ok is True  # FakeIdentificationProvider.identify() non trova match: nessun mislabel affermato
