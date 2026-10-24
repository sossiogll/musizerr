"""
Fase 4a: ``services/config_manager.py`` — ``ConfigManager`` è oggi una facade
sopra ``SettingsRepository`` (tabella ``settings``, scope 'system'), e
QUELLA tabella è l'unica fonte di verità a runtime — niente più fallback su
``config.json`` (che nessuno scrive più: un fallback su un file stantio
servirebbe silenziosamente un valore vecchio, peggio del default dichiarato
nel catalogo). Questi test la esercitano da fuori, come fa ogni call-site
reale (worker, routes, provider), mai toccando la tabella direttamente.
"""
from __future__ import annotations

import pytest

from services.config_manager import ConfigManager



@pytest.fixture
def config(engine):
    return ConfigManager(engine=engine)


# ---------------------------------------------------------------------------
# La facade risponde correttamente contro una tabella vuota (avvio a freddo)
# ---------------------------------------------------------------------------


def test_get_falls_back_to_catalog_default_against_an_empty_table(config):
    # `max_retries` è nel CATALOG reale con default 5 — nessuna riga in DB:
    # deve comunque rispondere, mai sollevare.
    assert config.get("max_retries") == 5
    assert config.max_retries == 5


def test_get_returns_caller_default_for_a_key_unknown_to_the_catalog(config):
    assert config.get("not_a_real_setting", "fallback") == "fallback"


def test_all_merges_catalog_defaults_with_an_empty_store(config):
    values = config.all()
    assert values["max_retries"] == 5
    assert values["save_cover"] is True


# ---------------------------------------------------------------------------
# Scrittura: round-trip immediato sulla STESSA istanza (invalidazione
# esplicita della cache, non un'attesa della finestra TTL).
# ---------------------------------------------------------------------------


def test_set_then_get_round_trips_immediately_on_the_same_instance(config):
    config.set("max_retries", 7)
    assert config.get("max_retries") == 7
    assert config.max_retries == 7


def test_update_writes_every_key_and_is_visible_immediately(config):
    config.update({"max_retries": 9, "save_cover": False})
    assert config.get("max_retries") == 9
    assert config.save_cover is False


def test_write_through_one_instance_is_visible_from_a_second_instance_sharing_the_engine(engine):
    writer = ConfigManager(engine=engine)
    reader = ConfigManager(engine=engine)

    writer.set("max_retries", 5)

    # Due processi (qui due istanze) che condividono lo stesso DB vedono la
    # scrittura dell'altro — qui immediatamente perché la cache del reader
    # parte vuota (mai ancora popolata) e va sempre a query al primo accesso.
    assert reader.get("max_retries") == 5


# ---------------------------------------------------------------------------
# Nessun fallback su file: una chiave assente dal DB è SEMPRE il default del
# catalogo, mai un valore letto da config.json (anche se il file esiste e la
# contiene) — il file non viene più nemmeno guardato a runtime.
# ---------------------------------------------------------------------------


def test_key_absent_from_db_uses_catalog_default_never_a_file(tmp_path, engine, monkeypatch):
    # Un config.json presente sul disco, con un valore che DIFFERISCE dal
    # default dichiarato: se ConfigManager lo leggesse ancora, questo test
    # lo rivelerebbe. MUSIZEER_DATA_DIR è patchato per assicurarsi che, se qualche
    # codice residuo tentasse comunque di leggere il file "di default", non
    # troverebbe per caso un file di un'altra run.
    monkeypatch.setenv("MUSIZEER_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text('{"public_base_url": "http://stale.example"}')

    config = ConfigManager(engine=engine)

    assert config.get("public_base_url") == ""  # default dichiarato nel catalogo


def test_writes_never_touch_config_json_on_disk(tmp_path, engine, monkeypatch):
    monkeypatch.setenv("MUSIZEER_DATA_DIR", str(tmp_path))
    path = tmp_path / "config.json"
    path.write_text('{"max_retries": 1}')
    original_raw = path.read_text()
    config = ConfigManager(engine=engine)

    config.set("max_retries", 42)

    assert path.read_text() == original_raw
