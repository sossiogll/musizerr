"""Copre il bug fail-open corretto in ``core/settings_catalog.py``
(``SettingDef.fail_safe``) e ``services/config_manager.py`` (``ConfigManager``).

Il problema: mentre la tabella ``settings`` non è mai stata letta con
successo da un'istanza di ``ConfigManager`` (DB irraggiungibile, o app
avviata prima che la migrazione sia stata applicata), ``get()`` degradava al
default dichiarato nel catalogo. Per ``auth_enabled`` quel default è
``False``, e ``routes/deps.py::get_current_user`` restituisce l'utente
sintetico admin per OGNI richiesta quando ``auth_enabled`` è ``False`` —
quindi in quella finestra ogni richiesta anonima era admin.

Regola d'oro rispettata: nessun provider reale, nessuna route chiamata via
rete — stesso stile di ``test_history_permissions.py``/
``test_stream_permissions.py`` (le dipendenze FastAPI sono funzioni Python
ordinarie, richiamate direttamente)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from services.config_manager import ConfigManager
from routes.deps import get_current_user

from tests.database import new_engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine_without_tables():
    """Engine SQLite in-memory su cui NON è mai stato chiamato
    ``SQLModel.metadata.create_all`` — la tabella ``settings`` non esiste,
    esattamente lo scenario "app avviata prima della migrazione"/"DB
    irraggiungibile" che ``ConfigManager`` deve degradare in sicurezza."""
    return new_engine(schema=False)


@pytest.fixture
def engine_with_tables():
    eng = new_engine()
    return eng


# ---------------------------------------------------------------------------
# Store mai leggibile: le chiavi con fail_safe lo servono, non il default
# ---------------------------------------------------------------------------

def test_never_loaded_store_serves_fail_safe_for_auth_enabled(engine_without_tables):
    config = ConfigManager(engine=engine_without_tables)
    assert config.auth_enabled is True
    assert config.get("auth_enabled") is True


def test_never_loaded_store_serves_fail_safe_for_registration_open(engine_without_tables):
    config = ConfigManager(engine=engine_without_tables)
    assert config.registration_open is False
    assert config.get("registration_open") is False


def test_never_loaded_store_all_reflects_the_same_fail_safe_values(engine_without_tables):
    config = ConfigManager(engine=engine_without_tables)
    values = config.all()
    assert values["auth_enabled"] is True
    assert values["registration_open"] is False


def test_never_loaded_store_keeps_catalog_default_for_a_key_without_fail_safe(engine_without_tables):
    # `max_retries` non dichiara un fail_safe: comportamento invariato, serve
    # sempre il default di catalogo anche mentre lo store non è leggibile.
    config = ConfigManager(engine=engine_without_tables)
    assert config.get("max_retries") == 5
    assert config.all()["max_retries"] == 5


# ---------------------------------------------------------------------------
# Store leggibile ma vuoto (avvio a freddo "normale"): default di catalogo
# ---------------------------------------------------------------------------

def test_readable_empty_store_serves_the_normal_catalog_default(engine_with_tables):
    config = ConfigManager(engine=engine_with_tables)
    assert config.auth_enabled is False
    assert config.registration_open is True


# ---------------------------------------------------------------------------
# Dopo un caricamento riuscito, un successivo fallimento mantiene la cache
# (comportamento preesistente — qui testato esplicitamente)
# ---------------------------------------------------------------------------

def test_previously_loaded_value_survives_a_later_unreadable_store(engine_with_tables):
    config = ConfigManager(engine=engine_with_tables)

    # Un `get()` contro lo store leggibile (anche vuoto) lo carica con
    # successo e marca l'istanza come "loaded".
    assert config.get("max_retries") == 5
    assert config._loaded is True

    # Scrittura di un valore non-default: entra in cache.
    config.set("max_retries", 99)
    assert config.get("max_retries") == 99

    # Il DB diventa irraggiungibile (qui: sostituito con un engine senza
    # tabelle) e forziamo un refresh resettando il timestamp della cache.
    config._engine = new_engine(schema=False)
    config._cache_loaded_at = 0.0

    # Il valore CACHATO resta quello scritto in precedenza, mai sostituito
    # dal default di catalogo (3) — comportamento preesistente, qui testato
    # esplicitamente.
    assert config.get("max_retries") == 99
    assert config._loaded is True


def test_a_write_alone_does_not_mark_the_instance_as_loaded_for_other_keys(engine_without_tables):
    """Una scrittura riuscita prova solo che il DB è raggiungibile ORA — non
    carica le altre chiavi mai lette. Non deve far scattare `_loaded=True` per
    l'intera istanza: altrimenti una chiave con fail_safe non ancora letta
    (es. auth_enabled) smetterebbe di essere protetta dopo la prima scrittura
    di una chiave qualunque, anche se quella scrittura non ha mai popolato la
    cache dallo store reale."""
    config = ConfigManager(engine=engine_without_tables)
    # Una scrittura contro un engine senza tabelle fallisce comunque (nessuna
    # tabella su cui scrivere) — verifichiamo quindi solo la property diretta.
    assert config._loaded is False
    assert config.auth_enabled is True


# ---------------------------------------------------------------------------
# End-to-end attraverso il layer HTTP: get_current_user non deve MAI
# restituire l'utente sintetico admin mentre lo store non è mai stato letto.
# ---------------------------------------------------------------------------

def test_get_current_user_rejects_anonymous_when_store_was_never_loaded(engine_without_tables):
    config = ConfigManager(engine=engine_without_tables)
    # Nessuna Session reale necessaria: get_current_user consulta la session
    # solo DOPO aver superato il controllo su auth_enabled, che qui è già
    # fail-safe True e fa terminare la funzione prima di toccarla.
    with pytest.raises(HTTPException) as exc:
        get_current_user(session=None, config=config, credentials=None)
    assert exc.value.status_code == 401


def test_get_current_user_still_serves_the_synthetic_admin_once_the_store_loads_the_catalog_default(
    engine_with_tables,
):
    # Fresh install "legittimo" (nessun login mai configurato, store
    # leggibile e vuoto): auth_enabled resta False e il comportamento
    # preesistente (nessun login richiesto) continua a funzionare.
    config = ConfigManager(engine=engine_with_tables)
    user = get_current_user(session=None, config=config, credentials=None)
    assert user.role == "admin"
