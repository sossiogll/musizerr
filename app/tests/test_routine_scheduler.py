"""``workers/scheduler.py`` — lo scheduler dinamico dei "system routines":
``tick`` (beat, ogni 60s) dispaccia i routine abilitati e scaduti tramite
``dispatch_routine``, che crea la riga ``routine_runs`` ``queued`` PRIMA di
accodare il task reale (mai dopo — è la finestra fra ``apply_async`` e
l'avvio effettivo del task a permettere un doppio dispaccio se la riga
comparisse solo all'avvio). ``run_routine`` è il wrapper che prende la riga
(``running``), esegue il task reale per NOME e la chiude. Il caso del
backlog (run in coda oltre il max runtime, periodo) è in
``test_routine_backlog.py``.

Segue lo stile di ``test_delegation_reconciliation.py``: ``workers.scheduler``
usa un ``engine`` di modulo (``from db.engine import engine``), monkeypatchato
su un DB SQLite isolato — mai il DB reale.
"""
from __future__ import annotations

import json

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session

from core.routines import RoutineDef, enabled_setting_key, interval_setting_key
from db.models import RoutineRun
from db.routine_run_store import finish_run, latest_run, queue_run
from db.settings_store import SettingsRepository
from workers import scheduler



@pytest.fixture
def isolated_engine(monkeypatch, engine):
    monkeypatch.setattr(scheduler, "engine", engine)
    return engine


def start_run(session, key: str) -> RoutineRun:
    """A run a worker has just started (queued and started now)."""
    now = datetime.now(timezone.utc)
    run = RoutineRun(routine_key=key, status="running", queued_at=now, started_at=now)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _dispatched_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        scheduler.run_routine, "apply_async",
        lambda kwargs=None, **opts: calls.append(kwargs["routine_key"]),
    )
    return calls


R1 = RoutineDef(key="r1", task_name="tests.fake.r1", default_interval_minutes=10, min_interval_minutes=1)
R2 = RoutineDef(key="r2", task_name="tests.fake.r2", default_interval_minutes=10, min_interval_minutes=1,
                default_enabled=False)
LONG_RUNNING = RoutineDef(key="long", task_name="tests.fake.long", default_interval_minutes=10,
                           min_interval_minutes=1, max_runtime_minutes=240)


# ---------------------------------------------------------------------------
# tick() — dispatch decisions
# ---------------------------------------------------------------------------


def test_tick_dispatches_a_never_run_enabled_routine(isolated_engine, monkeypatch):
    monkeypatch.setattr(scheduler, "ROUTINES", (R1,))
    calls = _dispatched_calls(monkeypatch)

    result = scheduler.tick()

    assert calls == ["r1"]
    assert result == {"dispatched": ["r1"]}


def test_tick_skips_a_disabled_routine(isolated_engine, monkeypatch):
    monkeypatch.setattr(scheduler, "ROUTINES", (R2,))
    calls = _dispatched_calls(monkeypatch)

    scheduler.tick()

    assert calls == []


def test_tick_skips_a_routine_not_yet_due(isolated_engine, monkeypatch, engine):
    monkeypatch.setattr(scheduler, "ROUTINES", (R1,))
    calls = _dispatched_calls(monkeypatch)

    with Session(engine) as session:
        run = start_run(session, "r1")
        finish_run(session, run.id, status="ok")  # started_at = adesso, intervallo 10 min

    scheduler.tick()

    assert calls == []


def test_tick_dispatches_once_the_interval_has_elapsed(isolated_engine, monkeypatch, engine):
    monkeypatch.setattr(scheduler, "ROUTINES", (R1,))
    calls = _dispatched_calls(monkeypatch)

    with Session(engine) as session:
        run = start_run(session, "r1")
        run.started_at = datetime.now(timezone.utc) - timedelta(minutes=15)
        session.add(run)
        session.commit()
        finish_run(session, run.id, status="ok")

    scheduler.tick()

    assert calls == ["r1"]


def test_tick_skips_a_routine_already_running(isolated_engine, monkeypatch, engine):
    monkeypatch.setattr(scheduler, "ROUTINES", (R1,))
    calls = _dispatched_calls(monkeypatch)

    with Session(engine) as session:
        run = start_run(session, "r1")
        run.started_at = datetime.now(timezone.utc) - timedelta(minutes=15)
        session.add(run)
        session.commit()
        # MAI finish_run: la run resta 'running' — un'esecuzione è ancora
        # in corso, il tick non deve duplicarla.

    scheduler.tick()

    assert calls == []


def test_tick_reaps_a_stale_running_row_then_dispatches_again(isolated_engine, monkeypatch, engine):
    monkeypatch.setattr(scheduler, "ROUTINES", (R1,))
    calls = _dispatched_calls(monkeypatch)

    with Session(engine) as session:
        run = start_run(session, "r1")
        run.started_at = datetime.now(timezone.utc) - timedelta(hours=2)
        session.add(run)
        session.commit()
        # 'running' da 2 ore: oltre il max_runtime_minutes di default (60) —
        # il worker che l'aveva avviata e' quasi certamente morto.

    scheduler.tick()

    assert calls == ["r1"]


def test_tick_respects_an_interval_change_without_restart(isolated_engine, monkeypatch, engine):
    """Il cuore della Fase 8: cambiare l'intervallo da PATCH /system/routines
    (che scrive nella tabella `settings`, come qualunque altra impostazione)
    ha effetto al PROSSIMO tick, senza reimportare/riavviare nulla."""
    monkeypatch.setattr(scheduler, "ROUTINES", (R1,))
    calls = _dispatched_calls(monkeypatch)

    with Session(engine) as session:
        run = start_run(session, "r1")
        run.started_at = datetime.now(timezone.utc) - timedelta(minutes=12)
        session.add(run)
        session.commit()
        finish_run(session, run.id, status="ok")

    scheduler.tick()
    assert calls == ["r1"]

    # tick ha appena creato una nuova run 'queued' (dispatch_routine, PRIMA
    # di apply_async) — la si avvia/retrodata/chiude qui per simulare
    # "iniziata e finita 5 minuti fa": e' l'avvio più recente, il tick deve
    # contare da questo, non da uno più vecchio.
    calls.clear()
    with Session(engine) as session:
        run = latest_run(session, "r1")
        run.status = "running"
        run.started_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.add(run)
        session.commit()
        finish_run(session, run.id, status="ok")
        SettingsRepository(session).set_system(interval_setting_key("r1"), 20)

    scheduler.tick()  # 5 min trascorsi, intervallo 10 -> non dovuto
    assert calls == []

    with Session(engine) as session:
        SettingsRepository(session).set_system(interval_setting_key("r1"), 3)

    scheduler.tick()  # stesso stato, solo l'intervallo e' cambiato -> ora dovuto
    assert calls == ["r1"]


def test_tick_skips_a_routine_disabled_via_settings_despite_default_enabled(isolated_engine, monkeypatch, engine):
    monkeypatch.setattr(scheduler, "ROUTINES", (R1,))
    calls = _dispatched_calls(monkeypatch)

    with Session(engine) as session:
        SettingsRepository(session).set_system(enabled_setting_key("r1"), False)

    scheduler.tick()

    assert calls == []


# ---------------------------------------------------------------------------
# dispatch_routine() — crea la riga PRIMA di accodare (fix della race):
# un tick il cui intervallo scade MENTRE il dispaccio precedente e' ancora
# in coda (worker occupato da un download) non deve raddoppiarlo.
# ---------------------------------------------------------------------------


def test_dispatch_routine_creates_the_run_row_before_apply_async(isolated_engine, monkeypatch, engine):
    monkeypatch.setattr(scheduler, "ROUTINES", (R1,))
    seen = {}

    def _fake_apply_async(kwargs=None, **opts):
        with Session(engine) as session:
            seen["status"] = latest_run(session, "r1").status
            seen["run_id"] = kwargs["run_id"]

    monkeypatch.setattr(scheduler.run_routine, "apply_async", _fake_apply_async)

    run = scheduler.dispatch_routine(R1)

    assert seen["status"] == "queued"
    assert seen["run_id"] == run.id


def test_two_consecutive_ticks_before_the_task_actually_starts_dispatch_once(isolated_engine, monkeypatch):
    """Le due chiamate a tick avvengono a distanza di zero secondi (nessun
    intervallo trascorso fra loro): gia' il controllo 'dovuto' basterebbe,
    ma e' comunque il caso letterale richiesto — nessun doppio dispaccio."""
    monkeypatch.setattr(scheduler, "ROUTINES", (R1,))
    calls = _dispatched_calls(monkeypatch)

    scheduler.tick()
    scheduler.tick()

    assert calls == ["r1"]


def test_a_still_queued_dispatch_blocks_a_second_one_once_its_interval_elapses(isolated_engine, monkeypatch, engine):
    """Il caso di race vero e proprio: il primo dispaccio resta 'queued'
    (il task reale non e' mai partito in questo test, worker occupato) e nel
    frattempo l'INTERVALLO scade — senza la riga creata PRIMA di
    apply_async, il tick successivo lo crederebbe libero e lo duplicherebbe."""
    monkeypatch.setattr(scheduler, "ROUTINES", (R1,))
    calls = _dispatched_calls(monkeypatch)

    scheduler.tick()
    assert calls == ["r1"]

    with Session(engine) as session:
        run = latest_run(session, "r1")
        run.queued_at = datetime.now(timezone.utc) - timedelta(minutes=15)  # oltre l'intervallo (10), sotto lo stale (60)
        session.add(run)
        session.commit()

    scheduler.tick()

    assert calls == ["r1"]  # nessun secondo dispaccio: ancora 'queued'


# ---------------------------------------------------------------------------
# max_runtime_minutes per-routine — un routine lungo (es. sync libreria) non
# va reaped/ri-dispacciato entro il suo stesso tempo di esecuzione plausibile.
# ---------------------------------------------------------------------------


def test_dispatch_routine_respects_a_longer_per_routine_max_runtime(isolated_engine, monkeypatch, engine):
    with Session(engine) as session:
        run = start_run(session, "long")
        run.started_at = datetime.now(timezone.utc) - timedelta(minutes=90)
        session.add(run)
        session.commit()

    calls = _dispatched_calls(monkeypatch)
    result = scheduler.dispatch_routine(LONG_RUNNING)

    assert result is None  # 90 min < 240 min: ancora una run sana, non reaped
    assert calls == []


def test_dispatch_routine_reaps_past_the_default_max_runtime(isolated_engine, monkeypatch, engine):
    with Session(engine) as session:
        run = start_run(session, "r1")
        run.started_at = datetime.now(timezone.utc) - timedelta(minutes=90)
        session.add(run)
        session.commit()

    calls = _dispatched_calls(monkeypatch)
    result = scheduler.dispatch_routine(R1)

    assert result is not None  # 90 min > 60 min di default: reaped e ri-dispacciato
    assert calls == ["r1"]


# ---------------------------------------------------------------------------
# run_routine() — esegue il task reale per NOME e chiude la riga già creata
# da dispatch_routine (non ne crea una propria).
# ---------------------------------------------------------------------------


def test_run_routine_unknown_key_leaves_the_run_row_untouched(isolated_engine, engine):
    with Session(engine) as session:
        run = start_run(session, "does-not-exist")

    result = scheduler.run_routine.run(routine_key="does-not-exist", run_id=run.id)

    assert result["status"] == "error"
    with Session(engine) as session:
        assert latest_run(session, "does-not-exist").status == "running"  # mai chiuso


def test_run_routine_records_ok_and_summarized_detail(isolated_engine, monkeypatch, engine):
    fake = RoutineDef(key="fake-ok", task_name="tests.fake.ok-task", default_interval_minutes=5)
    monkeypatch.setattr(scheduler, "get_routine", lambda key: fake if key == fake.key else None)

    @scheduler.celery.task(name=fake.task_name)
    def _fake_ok_task():
        return {"status": "done", "count": 3}

    with Session(engine) as session:
        run = queue_run(session, fake.key)

    result = scheduler.run_routine.run(routine_key=fake.key, run_id=run.id)

    assert result["status"] == "ok"
    # Stored as JSON, not as the Python repr: the page renders it as data.
    assert json.loads(result["detail"]) == {"status": "done", "count": 3}

    with Session(engine) as session:
        fetched = latest_run(session, fake.key)
        assert fetched.status == "ok"
        assert fetched.finished_at is not None


def test_run_routine_records_error_when_the_task_raises(isolated_engine, monkeypatch, engine):
    fake = RoutineDef(key="fake-boom", task_name="tests.fake.boom-task", default_interval_minutes=5)
    monkeypatch.setattr(scheduler, "get_routine", lambda key: fake if key == fake.key else None)

    @scheduler.celery.task(name=fake.task_name)
    def _fake_boom_task():
        raise RuntimeError("boom")

    with Session(engine) as session:
        run = queue_run(session, fake.key)

    result = scheduler.run_routine.run(routine_key=fake.key, run_id=run.id)

    assert result["status"] == "error"
    assert "boom" in result["detail"]

    with Session(engine) as session:
        fetched = latest_run(session, fake.key)
        assert fetched.status == "error"
        assert "boom" in fetched.detail
