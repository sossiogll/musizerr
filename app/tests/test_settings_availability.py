"""
Fase 5c (backend): disponibilità delle impostazioni end-to-end attraverso
``SettingsService`` — valutazione (``evaluate_availability``, Task 2),
enforcement in scrittura (``write_values``, Task 3) e test-before-switch di
un ``provider_ref`` (Task 4).

Regola d'oro rispettata: i provider usati sono fake locali a questo modulo
(o quelli condivisi di ``tests/conftest.py``), nessun nome di provider reale.
"""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import Session

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from services.config_manager import ConfigManager
from musizerr_sdk.interfaces import SyncProvider
from services.settings import SettingsService, SettingsWriteError
from db.models import User
from db.settings_store import SettingsRepository

from tests.conftest import FakeIdentificationProvider, FakeRegistry


# ---------------------------------------------------------------------------
# Fake locale: candidato provider_ref con test_connection controllabile
# (fallisce/solleva/passa) — usato per il Task 4.
# ---------------------------------------------------------------------------


class _FakeSyncCandidate(SyncProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name: str = "candidate", *, test_error: str | None = None, raise_on_test: bool = False):
        self.name = name
        self.label = name.title()
        self._test_error = test_error
        self._raise_on_test = raise_on_test
        self.test_connection_calls = 0

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session) -> str | None:
        self.test_connection_calls += 1
        if self._raise_on_test:
            raise RuntimeError("candidate unreachable — boom")
        return self._test_error

    def trigger_scan(self, session) -> None:
        return None

    def find_candidates(self, track, session):
        return []

    def get_stream_url(self, sync_id, session) -> str | None:
        return None


# ---------------------------------------------------------------------------
# Fixture (stesso pattern di test_settings_service.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def config(engine):
    return ConfigManager(engine=engine)


def _make_user(session, *, role: str) -> User:
    user = User(username=f"{role}-{uuid.uuid4()}", password_hash="x", role=role)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _service(session, config, **registry_kwargs) -> SettingsService:
    return SettingsService(session, config, FakeRegistry(**registry_kwargs))


# ---------------------------------------------------------------------------
# Task 1/2: category_has_usable_provider + evaluate_availability
# ---------------------------------------------------------------------------


def test_category_has_usable_provider_false_when_no_provider_registered(session, config):
    service = _service(session, config)
    assert service.category_has_usable_provider("identification") is False


def test_category_has_usable_provider_false_when_provider_unavailable(session, config):
    service = _service(session, config, identification=[FakeIdentificationProvider(available=False)])
    assert service.category_has_usable_provider("identification") is False


def test_category_has_usable_provider_true_when_one_usable_provider_exists(session, config):
    service = _service(
        session, config,
        identification=[
            FakeIdentificationProvider("a", config_valid=False),
            FakeIdentificationProvider("b", config_valid=True, available=True),
        ],
    )
    assert service.category_has_usable_provider("identification") is True


def test_evaluate_availability_reports_identification_settings_unavailable(session, config):
    admin = _make_user(session, role="admin")
    service = _service(session, config)  # nessun provider identification registrato

    availability = service.evaluate_availability(admin)

    available, reason = availability["identification_enabled"]
    assert available is False
    assert "identification" in reason
    available, reason = availability["anti_mislabel_enabled"]
    assert available is False


def test_evaluate_availability_reports_identification_settings_available(session, config):
    admin = _make_user(session, role="admin")
    service = _service(session, config, identification=[FakeIdentificationProvider(available=True)])

    availability = service.evaluate_availability(admin)

    assert availability["identification_enabled"] == (True, None)


def test_evaluate_availability_reports_sync_dependent_setting_unavailable_by_default(session, config):
    admin = _make_user(session, role="admin")
    service = _service(session, config)  # sync_provider mai impostato -> None

    available, reason = service.evaluate_availability(admin)["like_reconcile_interval_seconds"]
    assert available is False
    assert reason == "requires 'Sync provider' to be set"


def test_evaluate_availability_reports_sync_dependent_setting_available_once_sync_provider_set(session, config):
    admin = _make_user(session, role="admin")
    config.update({"sync_provider": "anysync"})
    service = _service(session, config)

    assert service.evaluate_availability(admin)["like_reconcile_interval_seconds"] == (True, None)


def test_evaluate_availability_for_nonadmin_never_exposes_the_sync_provider(session, config):
    # I requisiti possono referenziare "sync_provider" (scope='system'): un
    # non-admin non ne legge mai il valore.
    user = _make_user(session, role="user")
    service = _service(session, config)

    service.evaluate_availability(user)
    assert "sync_provider" not in service.effective_values(user)


# ---------------------------------------------------------------------------
# Task 3: enforcement in scrittura
# ---------------------------------------------------------------------------


def test_write_rejects_enabling_unavailable_setting_and_persists_nothing(session, config):
    admin = _make_user(session, role="admin")
    service = _service(session, config)  # nessun provider identification

    with pytest.raises(SettingsWriteError) as exc_info:
        service.write_values({"identification_enabled": True}, admin)

    assert exc_info.value.status_code == 400
    assert "identification_enabled" in exc_info.value.detail
    assert config.get("identification_enabled") is not True
    assert SettingsRepository(session).get_system("identification_enabled") is None


def test_write_allows_setting_default_back_even_when_unavailable(session, config):
    admin = _make_user(session, role="admin")
    service = _service(session, config)  # nessun provider identification -> unavailable

    # Il default dichiarato di identification_enabled è False: riscriverlo
    # deve sempre passare, anche se il requisito non è soddisfatto — è
    # l'unico modo per un utente di spegnere qualcosa diventato indisponibile.
    result = service.write_values({"identification_enabled": False}, admin)
    assert result["identification_enabled"] is False


def test_write_allows_enabling_setting_once_requirement_is_met(session, config):
    admin = _make_user(session, role="admin")
    service = _service(session, config, identification=[FakeIdentificationProvider(available=True)])

    result = service.write_values({"identification_enabled": True}, admin)
    assert result["identification_enabled"] is True


def test_write_batch_setting_sync_provider_and_dependent_setting_together_succeeds(session, config):
    admin = _make_user(session, role="admin")
    candidate = _FakeSyncCandidate("candidate")
    service = _service(session, config, syncs=[candidate])

    result = service.write_values({"sync_provider": "candidate", "like_reconcile_interval_seconds": 120}, admin)

    assert result["sync_provider"] == "candidate"
    assert SettingsRepository(session).get_system("like_reconcile_interval_seconds") == 120


def test_write_batch_clearing_sync_provider_while_enabling_dependent_setting_fails(session, config):
    admin = _make_user(session, role="admin")
    candidate = _FakeSyncCandidate("candidate")
    config.update({"sync_provider": "candidate"})
    service = _service(session, config, syncs=[candidate])

    with pytest.raises(SettingsWriteError) as exc_info:
        service.write_values({"sync_provider": "", "like_reconcile_interval_seconds": 120}, admin)

    assert exc_info.value.status_code == 400
    # Nulla del batch è stato scritto (unità atomica).
    assert config.get("sync_provider") == "candidate"
    assert SettingsRepository(session).get_system("like_reconcile_interval_seconds") is None


def test_a_previously_stored_enabled_value_survives_and_reads_as_unavailable(session, config):
    admin = _make_user(session, role="admin")
    available_provider = FakeIdentificationProvider(available=True)
    service = _service(session, config, identification=[available_provider])

    # Acceso mentre il requisito è soddisfatto.
    service.write_values({"identification_enabled": True}, admin)
    assert config.get("identification_enabled") is True

    # Il provider diventa indisponibile (es. dipendenza esterna sparita) —
    # nessuna scrittura nuova avviene: il valore salvato resta quello.
    available_provider._available = False

    values = service.effective_values(admin)
    assert values["identification_enabled"] is True  # mai riscritto/spento in automatico
    available, reason = service.evaluate_availability(admin)["identification_enabled"]
    assert available is False
    assert reason is not None


# ---------------------------------------------------------------------------
# Task 4: test-before-switch di un provider_ref
# ---------------------------------------------------------------------------


def test_provider_ref_switch_accepted_when_test_passes(session, config):
    admin = _make_user(session, role="admin")
    candidate = _FakeSyncCandidate("candidate", test_error=None)
    service = _service(session, config, syncs=[candidate])

    result = service.write_values({"sync_provider": "candidate"}, admin)

    assert result["sync_provider"] == "candidate"
    assert candidate.test_connection_calls == 1


def test_provider_ref_switch_rejected_when_test_fails_and_writes_nothing(session, config):
    admin = _make_user(session, role="admin")
    candidate = _FakeSyncCandidate("candidate", test_error="wrong password")
    service = _service(session, config, syncs=[candidate])

    with pytest.raises(SettingsWriteError) as exc_info:
        service.write_values({"sync_provider": "candidate"}, admin)

    assert exc_info.value.status_code == 400
    assert "wrong password" in exc_info.value.detail
    assert candidate.test_connection_calls == 1
    assert config.get("sync_provider") is None


def test_provider_ref_switch_rejected_when_test_raises_and_writes_nothing(session, config):
    admin = _make_user(session, role="admin")
    candidate = _FakeSyncCandidate("candidate", raise_on_test=True)
    service = _service(session, config, syncs=[candidate])

    # Un provider che solleva conta come test fallito — mai un errore non
    # gestito (niente RuntimeError propagata, solo SettingsWriteError 400).
    with pytest.raises(SettingsWriteError) as exc_info:
        service.write_values({"sync_provider": "candidate"}, admin)

    assert exc_info.value.status_code == 400
    assert "candidate unreachable" in exc_info.value.detail
    assert config.get("sync_provider") is None


def test_provider_ref_clearing_to_empty_does_not_run_test(session, config):
    admin = _make_user(session, role="admin")
    candidate = _FakeSyncCandidate("candidate")
    config.update({"sync_provider": "candidate"})
    service = _service(session, config, syncs=[candidate])

    result = service.write_values({"sync_provider": ""}, admin)

    assert not result.get("sync_provider")
    assert candidate.test_connection_calls == 0


def test_provider_ref_rewriting_same_value_does_not_run_test(session, config):
    admin = _make_user(session, role="admin")
    candidate = _FakeSyncCandidate("candidate")
    config.update({"sync_provider": "candidate"})
    service = _service(session, config, syncs=[candidate])

    result = service.write_values({"sync_provider": "candidate"}, admin)

    assert result["sync_provider"] == "candidate"
    assert candidate.test_connection_calls == 0
