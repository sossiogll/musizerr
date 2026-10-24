"""GET/PATCH /settings: policy admin-only per lo scope dichiarato, e
GET /providers / find_provider_by_name: comportamento sulle categorie.

Regola d'oro rispettata: nessun nome di provider concreto qui, solo
`delegation_webhook_token` (l'unica voce `secret=True` nel catalogo reale),
usata come istanza per verificare che il comportamento sia guidato dal flag
del catalogo e non da un nome di chiave hardcoded.

Storia: questo modulo pinnava GET/PATCH /config e PATCH /providers/{name}/config
(il percorso legacy). Entrambi sono stati rimossi — la stessa policy di
lettura/scrittura per scope vive oggi SOLO in GET/PATCH /settings (stesso
``SettingsService`` dietro entrambi i percorsi, prima e dopo la rimozione),
quindi i test che pinnavano un comportamento ANCORA presente sono stati
ri-espressi contro `get_settings_values`/`patch_settings`. L'unico test
rimosso per intero (non migrato) è quello che pinnava `_redacted_for` —
la seconda rete di sicurezza "svuota un segreto anche se lo scope lo
renderebbe leggibile", che non ha mai avuto un equivalente sul percorso
generico (lì un segreto system-scoped è semplicemente OMESSO da can_read,
mai svuotato): non c'è più alcun comportamento da pinnare.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from services.config_manager import ConfigManager
from core.settings_resolver import PROVIDER_CATEGORY_GETTERS, find_provider_by_name
from db.models import User
from routes.config import (
    SettingsPatch,
    get_settings_values,
    list_providers,
    patch_settings,
)

from tests.conftest import (
    FakeDownloadProvider,
    FakeIdentificationProvider,
    FakeMetadataProvider,
    FakeRegistry,
)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def config(engine):
    # Fase 4a: ConfigManager e' una facade sopra la tabella `settings` —
    # condivide lo stesso engine del fixture `session` cosi' le route sotto
    # test vedono lo stesso DB. `delegation_webhook_token` è seminato tramite
    # l'API pubblica (`update`, che scrive nella tabella): config.json non è
    # più letto a runtime, solo la migrazione lo legge.
    cm = ConfigManager(engine=engine)
    cm.update({"delegation_webhook_token": "s3cr3t-token"})
    return cm


@pytest.fixture
def registry():
    # GET/PATCH /settings includono anche i provider per default
    # (`include_providers=True`), ma nessun test qui esercita una chiave di
    # provider: un registry vuoto basta.
    return FakeRegistry()


def _make_user(session, *, role: str) -> User:
    user = User(username=f"{role}-user", password_hash="x", role=role)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_non_admin_does_not_receive_secret_value(config, registry, session):
    # Fase 3b: `delegation_webhook_token` è scope="system" nel CATALOG reale,
    # quindi un non-admin ora non lo vede PROPRIO (omesso da can_read), non
    # solo "vuoto" come nel vecchio comportamento — questa omissione è la
    # correzione di sicurezza voluta esplicitamente da questa fase.
    user = _make_user(session, role="user")
    data = get_settings_values(config, registry, session, user)
    assert "delegation_webhook_token" not in data


def test_admin_sees_secret_value(config, registry, session):
    admin = _make_user(session, role="admin")
    data = get_settings_values(config, registry, session, admin)
    assert data["delegation_webhook_token"] == "s3cr3t-token"


def test_system_scoped_non_secret_value_omitted_for_non_admin_but_visible_for_admin(config, registry, session):
    # `save_cover` non è un segreto ma è comunque scope="system": stesso
    # fix di sicurezza di sopra, su una chiave non-segreta qualunque.
    user = _make_user(session, role="user")
    admin = _make_user(session, role="admin")
    assert "save_cover" not in get_settings_values(config, registry, session, user)
    assert get_settings_values(config, registry, session, admin)["save_cover"] == config.get("save_cover")


def test_non_admin_patch_mixing_allowed_and_disallowed_is_rejected(config, registry, session):
    from db.settings_store import SettingsRepository

    user = _make_user(session, role="user")
    original_values = config.all()
    original_priority = SettingsRepository(session).get_user("metadata_provider_priority", user.id)

    body = SettingsPatch(metadata_provider_priority=["a", "b"], track_format="{title}")
    with pytest.raises(HTTPException) as exc_info:
        patch_settings(body, config, registry, session, user)

    assert exc_info.value.status_code == 403
    assert "track_format" in exc_info.value.detail

    # Nulla scritto: né la tabella `settings` (system-scope) né l'override
    # personale dell'utente (settings, scope='user').
    assert config.all() == original_values
    assert SettingsRepository(session).get_user("metadata_provider_priority", user.id) == original_priority


def test_non_admin_patch_with_empty_body_is_a_200_noop(config, registry, session):
    # Deliberate behaviour (not previously specified): an empty patch from a
    # non-admin requests nothing forbidden, so it is a no-op that returns 200
    # with the current config rather than a 403 — unlike the old code, which
    # raised 403 whenever the filtered update dict was empty. Pin it here so
    # it can't silently flip back.
    from db.settings_store import SettingsRepository

    user = _make_user(session, role="user")
    original_values = config.all()
    original_priority = SettingsRepository(session).get_user("metadata_provider_priority", user.id)

    body = SettingsPatch()
    data = patch_settings(body, config, registry, session, user)

    # track_format è scope="system": omesso per un non-admin (fix di Fase 3b),
    # non più presente col suo valore come nel vecchio comportamento.
    assert "track_format" not in data
    assert config.all() == original_values
    assert SettingsRepository(session).get_user("metadata_provider_priority", user.id) == original_priority


def test_non_admin_patch_reports_all_disallowed_keys_sorted(config, registry, session):
    user = _make_user(session, role="user")

    body = SettingsPatch(track_format="{title}", download_provider_order=["x"])
    with pytest.raises(HTTPException) as exc_info:
        patch_settings(body, config, registry, session, user)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Admin role required for: download_provider_order, track_format"


def test_non_admin_patch_with_only_allowed_key_succeeds(config, registry, session):
    # Fase 4a: l'override personale vive nella tabella `settings` (scope
    # 'user'). Fase 4b ha droppato la colonna `User.metadata_provider_priority`
    # — non esiste più nemmeno come schema (vedi la stessa nota in
    # test_settings_service.py).
    from db.settings_store import SettingsRepository
    from tests.conftest import FakeMetadataProvider

    # The answer is the EFFECTIVE order (registered providers only), so they must exist.
    registry = FakeRegistry(metadata=[FakeMetadataProvider("p2"), FakeMetadataProvider("p1")])
    user = _make_user(session, role="user")

    body = SettingsPatch(metadata_provider_priority=["p1", "p2"])
    data = patch_settings(body, config, registry, session, user)

    assert data["metadata_provider_priority"] == ["p1", "p2"]
    assert SettingsRepository(session).get_user("metadata_provider_priority", user.id) == ["p1", "p2"]
    # Non finisce nel default globale (system-scope), resta override personale.
    assert config.get("metadata_provider_priority") == []


def test_admin_patch_with_admin_only_key_succeeds(config, registry, session):
    # Fase 4a: la scrittura passa per la tabella `settings` (via
    # ConfigManager, che ne è oggi una facade) — config.json non esiste
    # nemmeno più come concetto per ConfigManager, la verifica passa quindi
    # solo da `config.get(...)` (l'API pubblica invariata di ConfigManager).
    admin = _make_user(session, role="admin")

    body = SettingsPatch(track_format="{title}")
    data = patch_settings(body, config, registry, session, admin)

    assert data["track_format"] == "{title}"
    assert config.get("track_format") == "{title}"


# ---------------------------------------------------------------------------
# GET /providers: la risposta copre OGNI categoria che il registry conosce
# (Fase 5a, fix 2) — derivata da PROVIDER_CATEGORY_GETTERS, la stessa lista
# usata da merged_schema per costruire lo schema unificato, non riscritta a
# mano qui. Prima di questo fix "identification" aveva impostazioni proprie
# nello schema unificato ma non compariva affatto in questa risposta.
# ---------------------------------------------------------------------------


def test_list_providers_covers_every_category_the_registry_declares(config, session):
    admin = _make_user(session, role="admin")
    registry = FakeRegistry(identification=[FakeIdentificationProvider("fakeident")])

    data = list_providers(registry, config, session, admin)

    expected_categories = {name.removeprefix("all_") for name in PROVIDER_CATEGORY_GETTERS}
    assert set(data) == expected_categories
    assert "identification" in data, "una categoria che il registry conosce non può mancare dalla risposta"
    assert [p["name"] for p in data["identification"]] == ["fakeident"]
    assert data["identification"][0]["label"] == "Fakeident"


def test_list_providers_empty_category_is_an_empty_list_not_a_missing_key(config, session):
    # Nessun provider registrato in una data categoria (es. nessuna
    # "lyrics" installata): la chiave resta presente con una lista vuota, non
    # sparisce — cosi' il frontend può sempre iterare ogni categoria senza
    # doverne controllare la presenza.
    admin = _make_user(session, role="admin")
    registry = FakeRegistry()

    data = list_providers(registry, config, session, admin)

    assert data["identification"] == []
    assert data["lyrics"] == []


# ---------------------------------------------------------------------------
# find_provider_by_name (core.settings_resolver): deve cercare in TUTTE le
# categorie che il registry dichiara (derivate da PROVIDER_CATEGORY_GETTERS),
# non in una lista scritta a mano — prima di questo fix "identification" ne
# era assente, quindi qualunque route che passa da qui per un provider di
# quella categoria (es. AcoustID) falliva sempre con 404. Storia: questo
# comportamento era pinnato passando da PATCH /providers/{name}/config
# (save_provider_config, oggi rimosso) contro una copia privata
# routes.config._find_provider; quella copia e la sua gemella in
# routes.auth._get_provider sono state sostituite da questa UNICA funzione
# condivisa (routes.config::delete_provider_account e ogni endpoint di
# routes/auth.py la richiamano entrambi ora) — testata direttamente qui.
# ---------------------------------------------------------------------------


def test_find_provider_searches_every_category_the_registry_declares(session):
    registry = FakeRegistry(identification=[FakeIdentificationProvider("fakeident")])

    # Non deve tornare None: prima del fix, find_provider_by_name (e le sue
    # due copie precedenti) non guardava affatto nella categoria
    # "identification".
    assert find_provider_by_name(registry, "fakeident") is not None


def test_find_provider_returns_none_for_a_name_in_no_category(session):
    registry = FakeRegistry()

    assert find_provider_by_name(registry, "does-not-exist") is None


# ---------------------------------------------------------------------------
# GET /providers: un provider con auth.flow == NONE non ha login da tentare,
# quindi resta sempre "authenticated" — ma questo non vuol dire che sia
# davvero utilizzabile. Il campo additivo "usable" (Fase 5b fix 4) combina
# quel segnale con la stessa domanda di completezza già esposta da
# "config_valid" e, per i provider di identificazione, con il loro stesso
# is_available(): un provider senza login ma con config incompleta o
# indisponibile non deve risultare "connesso".
# ---------------------------------------------------------------------------


def test_provider_with_no_login_and_invalid_config_is_not_usable(config, session):
    admin = _make_user(session, role="admin")
    registry = FakeRegistry(
        identification=[FakeIdentificationProvider("fakeident", config_valid=False)]
    )

    data = list_providers(registry, config, session, admin)
    info = data["identification"][0]

    assert info["authenticated"] is True, "flow NONE: nessun login da tentare, sempre vero"
    assert info["config_valid"] is False
    assert info["usable"] is False, "config incompleta: non deve risultare connesso"


def test_provider_with_no_login_and_unavailable_backend_is_not_usable(config, session):
    admin = _make_user(session, role="admin")
    registry = FakeRegistry(
        identification=[FakeIdentificationProvider("fakeident", available=False)]
    )

    data = list_providers(registry, config, session, admin)
    info = data["identification"][0]

    assert info["authenticated"] is True
    assert info["config_valid"] is True
    assert info["usable"] is False, "is_available() del contratto identification dice di no"


def test_provider_with_no_login_and_valid_usable_config_is_usable(config, session):
    admin = _make_user(session, role="admin")
    registry = FakeRegistry(identification=[FakeIdentificationProvider("fakeident")])

    data = list_providers(registry, config, session, admin)
    info = data["identification"][0]

    assert info["authenticated"] is True
    assert info["config_valid"] is True
    assert info["usable"] is True


# ---------------------------------------------------------------------------
# GET /providers: "also_registered_in" — identità per OGGETTO, mai per nome
# (Fase 5c, Task 4, fix di regressione). Il frontend deve poter distinguere
# un provider genuinamente registrato in più categorie (un solo oggetto, una
# sola config salvata) da una collisione di `name` fra due CLASSI DISTINTE
# registrate ciascuna in una categoria diversa — la forma esatta del
# provider reale che riempie due ruoli con due classi (metadata e download sono due classi separate che
# condividono il nome). Il frontend NON può fare questa distinzione da solo:
# deve arrivare dal backend, che conosce l'identità degli oggetti.
# ---------------------------------------------------------------------------


def test_also_registered_in_reports_the_same_object_across_categories(config, session):
    admin = _make_user(session, role="admin")
    shared = FakeMetadataProvider("dualrole")
    # FakeMetadataProvider non dichiara `auth` (i test di ricerca non passano
    # mai da _provider_info) — qui invece serve, come per gli altri fake
    # provider di questo modulo.
    shared.auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    # STESSO oggetto Python passato a due categorie diverse — il caso
    # genuino (es. un provider sia metadata sia sync).
    registry = FakeRegistry(metadata=[shared], syncs=[shared])

    data = list_providers(registry, config, session, admin)

    assert data["metadata"][0]["name"] == "dualrole"
    assert data["metadata"][0]["also_registered_in"] == ["sync"]
    assert data["sync"][0]["name"] == "dualrole"
    assert data["sync"][0]["also_registered_in"] == ["metadata"]


def test_also_registered_in_does_not_conflate_two_distinct_providers_sharing_a_name(config, session):
    admin = _make_user(session, role="admin")
    # DUE oggetti DISTINTI (classi diverse), che solo CONDIVIDONO `name` —
    # non l'identità dichiarata da list_providers, quindi ciascuno deve
    # riportare "also_registered_in" vuoto: nessuno dei due è "anche"
    # nell'altra categoria, sono provider indipendenti.
    distinct_metadata = FakeMetadataProvider("samename")
    distinct_metadata.auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    registry = FakeRegistry(
        metadata=[distinct_metadata],
        downloads=[FakeDownloadProvider("samename")],
    )

    data = list_providers(registry, config, session, admin)

    assert data["metadata"][0]["name"] == "samename"
    assert data["metadata"][0]["also_registered_in"] == []
    assert data["download"][0]["name"] == "samename"
    assert data["download"][0]["also_registered_in"] == []


# ---------------------------------------------------------------------------
# library_available: dichiarato dal contratto, riportato solo da chi dipende
# da una libreria esterna.
# ---------------------------------------------------------------------------


class _NeedsLibrary(FakeIdentificationProvider):
    def __init__(self, installed: bool) -> None:
        super().__init__("needslib")
        self._installed = installed

    def is_library_available(self) -> bool:
        return self._installed


@pytest.mark.parametrize("installed", [True, False])
def test_a_provider_that_needs_a_library_reports_whether_it_is_installed(config, session, installed):
    admin = _make_user(session, role="admin")

    info = list_providers(FakeRegistry(identification=[_NeedsLibrary(installed)]), config, session, admin)["identification"][0]

    assert info["library_available"] is installed


def test_a_provider_without_a_library_dependency_reports_nothing(config, session):
    admin = _make_user(session, role="admin")

    info = list_providers(FakeRegistry(identification=[FakeIdentificationProvider("plain")]), config, session, admin)["identification"][0]

    assert "library_available" not in info
