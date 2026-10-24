"""
``services/settings.py`` — l'orchestratore fra le route HTTP e i tre storage
esistenti (config.json, credential store per-provider, riga User per
l'unico override personale).

Regola d'oro rispettata: nessun provider reale nominato. Il provider usato per
esercitare il routing verso il credential store e ``validate_config`` è un
fake locale a questo modulo, provider-agnostico.
"""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import Session

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from services.config_manager import ConfigManager
from musizerr_sdk.interfaces import MetadataProvider
from musizerr_sdk.settings import ProviderSetting
from services.settings import SettingsService, SettingsWriteError
from db import user_service
from db.models import SYSTEM_USER_ID, User
from db.settings_store import SettingsRepository

from tests.conftest import FakeRegistry
from tests.loopback import as_proxy


# ---------------------------------------------------------------------------
# Fake provider locale — nessun nome di provider reale
# ---------------------------------------------------------------------------


class _FakeConfigurableProvider(MetadataProvider):
    """Provider finto con config_schema esplicito e validate_config
    controllabile dal test, per esercitare il routing verso il credential
    store e il contratto validate_config (mai invocato prima di questa fase)."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name: str = "fakeprovider", *, defs=None, valid: bool = True) -> None:
        self.name = name
        self.label = name.title()
        self._defs = defs if defs is not None else [
            ProviderSetting(key="search_limit", type="int", default=20, label="Limit", minimum=1, maximum=100),
            ProviderSetting(key="url", type="string", default="", label="URL", required=False),
        ]
        self._valid = valid
        self.validate_config_calls: list[dict] = []

    def config_schema(self):
        return list(self._defs)

    def validate_config(self, config: dict) -> bool:
        self.validate_config_calls.append(dict(config))
        return self._valid

    def test_connection(self, session) -> str | None:
        return None


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def config(engine):
    # Fase 4a: ConfigManager e' una facade sopra la stessa tabella `settings`
    # che SettingsService raggiunge via SettingsRepository — condivide quindi
    # lo STESSO engine del fixture `session` (un DB SQLite in-memory dedicato
    # a questo modulo di test), cosi' le scritture fatte attraverso l'uno
    # sono visibili attraverso l'altro, esattamente come i due processi reali
    # (app/worker) condividono lo stesso DB in produzione.
    return ConfigManager(engine=engine)


@pytest.fixture
def provider():
    return _FakeConfigurableProvider()


@pytest.fixture
def registry(provider):
    return FakeRegistry(metadata=[provider])


@pytest.fixture
def service(session, config, registry):
    return SettingsService(session, config, registry)


def _make_user(session, *, role: str) -> User:
    # Username univoco: alcuni test creano più utenti con lo stesso ruolo
    # nella stessa sessione (es. due non-admin per verificare l'indipendenza
    # dei rispettivi override personali).
    user = User(username=f"{role}-{uuid.uuid4()}", password_hash="x", role=role)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Lettura: omissione delle chiavi system-scoped, override visibile
# ---------------------------------------------------------------------------


def test_ordinary_user_values_omit_system_scoped_settings(service, session):
    user = _make_user(session, role="user")
    values = service.effective_values(user, include_providers=False)

    # track_format è scope="system" nel CATALOG reale: mai per un non-admin.
    assert "track_format" not in values
    assert "delegation_webhook_token" not in values
    # metadata_provider_priority è system_with_user_override: sempre leggibile,
    # anche senza un override personale ancora impostato. Il valore è l'ordine
    # EFFETTIVO (services/provider_order): mai la lista grezza, vuota di default,
    # ma tutti i provider di metadati registrati.
    assert values["metadata_provider_priority"] == ["fakeprovider"]


def test_admin_values_include_system_scoped_settings(service, session):
    admin = _make_user(session, role="admin")
    values = service.effective_values(admin, include_providers=False)
    assert "track_format" in values


# ---------------------------------------------------------------------------
# Scrittura: rifiuti — nulla viene persistito in nessuno dei casi
# ---------------------------------------------------------------------------


def test_write_rejects_unknown_key_and_persists_nothing(service, session, config):
    admin = _make_user(session, role="admin")
    original_values = config.all()

    with pytest.raises(SettingsWriteError) as exc_info:
        service.write_values({"totally_unknown_setting": 1}, admin)

    assert exc_info.value.status_code == 400
    assert "totally_unknown_setting" in exc_info.value.detail
    assert config.all() == original_values


def test_write_rejects_forbidden_key_for_role_and_persists_nothing(service, session, config):
    user = _make_user(session, role="user")
    original_values = config.all()

    with pytest.raises(SettingsWriteError) as exc_info:
        service.write_values({"track_format": "{title}"}, user)

    assert exc_info.value.status_code == 403
    assert "track_format" in exc_info.value.detail
    assert config.all() == original_values


def test_write_rejects_uncoercible_value_and_persists_nothing(service, session, config):
    admin = _make_user(session, role="admin")
    original_values = config.all()

    with pytest.raises(SettingsWriteError) as exc_info:
        service.write_values({"max_retries": "not-a-number"}, admin)

    assert exc_info.value.status_code == 400
    assert "max_retries" in exc_info.value.detail
    assert config.all() == original_values


def test_write_rejects_out_of_bounds_value_and_persists_nothing(service, session, config):
    admin = _make_user(session, role="admin")
    original_values = config.all()

    with pytest.raises(SettingsWriteError) as exc_info:
        service.write_values({"max_retries": 999}, admin)  # maximum=10 nel CATALOG

    assert exc_info.value.status_code == 400
    assert "max_retries" in exc_info.value.detail
    assert config.all() == original_values


def test_write_rejects_whole_batch_when_one_key_is_bad_and_writes_nothing(service, session, config):
    # Unità atomica: una chiave valida insieme a una cattiva non deve scrivere
    # nemmeno la chiave valida.
    admin = _make_user(session, role="admin")
    original_values = config.all()

    with pytest.raises(SettingsWriteError):
        service.write_values({"track_format": "{title}", "max_retries": 999}, admin)

    assert config.all() == original_values
    assert config.get("track_format") != "{title}"


# ---------------------------------------------------------------------------
# Scrittura: routing verso il provider (short key). NOTA (post-review): scritto
# per la prima volta ``validate_config`` era invocato come gate di scrittura
# qui — rimosso perché risponde a una domanda di COMPLETEZZA sull'INTERO
# provider (spesso richiede credenziali salvate altrove, es. client
# id/secret), non "questo campo è accettabile?". Un provider REALE non ancora
# completamente configurato deve poter accettare comunque un aggiornamento
# innocuo come `search_limit`. Il contratto si sposta sulla lettura — vedi
# la sezione "GET /providers: config_valid" più sotto.
#
# NOTA (Fase 4a): questi test ispezionavano lo storage tramite
# ``CredentialStore(session, user_id=None).get_provider_config(...)`` — quello
# ERA lo storage per ``provider_config`` in Fase 3b. Questa fase lo sposta
# nella tabella `settings` (namespaced, una riga per short key): l'assert sul
# valore atteso resta lo stesso, solo il punto di ispezione cambia per
# puntare al nuovo storage reale, altrimenti il test passerebbe verificando
# un percorso ormai morto (``provider_config`` non riceve più scritture).
# ---------------------------------------------------------------------------


def test_provider_setting_written_through_service_lands_under_short_key(service, session, provider):
    admin = _make_user(session, role="admin")
    key = f"provider.{provider.name}.search_limit"

    service.write_values({key: 42}, admin)

    assert SettingsRepository(session).get_system(key) == 42
    assert service.provider_config(provider.name) == {"search_limit": 42}
    # validate_config NON è più un gate di scrittura: non deve essere invocato qui.
    assert provider.validate_config_calls == []


def test_partial_write_persists_even_when_provider_validate_config_would_return_false(session, config, provider):
    # Il caso reale che ha causato la regressione: un provider non ancora
    # completamente configurato (validate_config -> False, es. manca un
    # client id/secret che vive nell'oauth client store) deve comunque poter
    # accettare un aggiornamento parziale e innocuo come `search_limit`.
    provider._valid = False
    registry = FakeRegistry(metadata=[provider])
    service = SettingsService(session, config, registry)
    admin = _make_user(session, role="admin")
    key = f"provider.{provider.name}.search_limit"

    result = service.write_values({key: 42}, admin)

    assert result[key] == 42
    assert SettingsRepository(session).get_system(key) == 42
    # E il gate di scrittura non ha nemmeno provato a chiamare validate_config.
    assert provider.validate_config_calls == []
    # Il verdetto di completezza resta disponibile, ma solo in lettura.
    assert service.provider_config_is_valid(as_proxy(provider)) is False


def test_provider_setting_written_through_service_is_immediately_visible_to_every_consumer(
    service, session, provider,
):
    # Questa è l'assertion la cui assenza ha lasciato passare la
    # regressione: dodici call-site reali (auth_manager, i provider stessi,
    # i worker, le route di ricerca/stream) leggevano la config di un
    # provider da ``CredentialStore.get_provider_config`` — uno storage
    # diverso da quello scritto da SettingsService. ``CredentialStore`` non
    # ha più (Fase 4a, seconda iterazione) alcun metodo di config: quella
    # classe è tornata a essere dedicata ai soli segreti. Ogni consumatore
    # reale legge ora tramite ``self.context.settings()`` — il
    # ``HostProviderContext`` di un provider (``services/provider_context.py``)
    # — o direttamente tramite ``SettingsRepository`` +
    # ``provider_config_from_system_settings`` (route/worker, che ne hanno
    # già una) — questo test esercita ENTRAMBI i percorsi contro lo stesso
    # valore scritto dal service. ``_get_provider_config`` è l'esatta funzione
    # dietro ``HostProviderContext.settings()``: chiamata qui con la
    # ``session`` di test (isolata) invece che senza — come fa
    # ``HostProviderContext``, che non ne ha mai una propria — per non
    # colpire l'engine di produzione condiviso da questo processo di test.
    from core.settings_resolver import provider_config_from_system_settings
    from services.provider_context import _get_provider_config

    admin = _make_user(session, role="admin")
    key = f"provider.{provider.name}.search_limit"

    service.write_values({key: 42}, admin)

    via_service = service.provider_config(provider.name)
    via_provider_accessor = _get_provider_config(provider.name, session=session)
    via_repository_direct = provider_config_from_system_settings(
        provider.name, SettingsRepository(session).all_system(),
    )
    assert via_service == via_provider_accessor == via_repository_direct == {"search_limit": 42}


# ---------------------------------------------------------------------------
# GET /providers: config_valid — validate_config si sposta sulla lettura
# ---------------------------------------------------------------------------


def test_provider_config_is_valid_reflects_validate_config_result(service, provider):
    provider._valid = True
    assert service.provider_config_is_valid(as_proxy(provider)) is True

    provider._valid = False
    assert service.provider_config_is_valid(as_proxy(provider)) is False


def test_provider_config_is_valid_treats_raising_validate_config_as_not_valid(session, config):
    class _RaisingProvider(_FakeConfigurableProvider):
        def validate_config(self, config: dict) -> bool:
            raise RuntimeError("boom — dipendenza esterna non disponibile")

    provider = _RaisingProvider("raisingprovider")
    registry = FakeRegistry(metadata=[provider])
    service = SettingsService(session, config, registry)

    # Non deve propagare l'eccezione: un provider che rompe validate_config
    # non deve mai far fallire l'intero GET /providers.
    assert service.provider_config_is_valid(as_proxy(provider)) is False


# ---------------------------------------------------------------------------
# system_with_user_override: admin -> default globale, non-admin -> override
# ---------------------------------------------------------------------------


def test_admin_write_of_overridable_key_sets_global_default(service, session, config):
    admin = _make_user(session, role="admin")

    service.write_values({"metadata_provider_priority": ["a", "b"]}, admin)

    assert config.get("metadata_provider_priority") == ["a", "b"]
    # Nessun override personale creato per l'admin: e' finito nel default globale.
    assert SettingsRepository(session).get_user("metadata_provider_priority", admin.id) is None


def test_non_admin_write_of_overridable_key_sets_personal_override_only(service, session, config):
    # Fase 4a: l'override personale vive nella tabella `settings` (scope
    # 'user'). Fase 4b ha droppato la colonna `User.metadata_provider_priority`
    # che la ospitava prima — questo test pin esplicitamente che la scrittura
    # continua ad andare SOLO nel nuovo storage (regressione reale da evitare:
    # vedi routes/deps.resolve_metadata_priority, che legge dallo stesso posto).
    user = _make_user(session, role="user")

    service.write_values({"metadata_provider_priority": ["x", "y"]}, user)

    # Non finisce nel default globale (system-scope), resta override personale.
    assert config.get("metadata_provider_priority") == []
    assert SettingsRepository(session).get_user("metadata_provider_priority", user.id) == ["x", "y"]


def test_two_users_overrides_are_independent_and_admin_default_is_the_fallback(session, config):
    registry = FakeRegistry(
        metadata=[_FakeConfigurableProvider("one"), _FakeConfigurableProvider("two"), _FakeConfigurableProvider("three")],
    )
    service = SettingsService(session, config, registry)
    admin = _make_user(session, role="admin")
    user_a = _make_user(session, role="user")
    user_b = _make_user(session, role="user")

    service.write_values({"metadata_provider_priority": ["three"]}, admin)
    service.write_values({"metadata_provider_priority": ["two"]}, user_a)

    values_a = service.effective_values(user_a, include_providers=False)
    values_b = service.effective_values(user_b, include_providers=False)

    # Each order is completed with the providers it does not name.
    assert values_a["metadata_provider_priority"] == ["two", "one", "three"]
    # user_b non ha un override personale: eredita il default globale appena scritto dall'admin.
    assert values_b["metadata_provider_priority"] == ["three", "one", "two"]


# ---------------------------------------------------------------------------
# scope='user' puro (Fase 4b): language, theme — a
# differenza di 'system_with_user_override' qui non esiste MAI un default
# globale: l'unica riga possibile e' quella dell'utente stesso.
# ---------------------------------------------------------------------------


def test_user_scope_write_lands_as_user_row_never_system(service, session, config):
    user = _make_user(session, role="user")

    service.write_values({"theme": "dark"}, user)

    assert SettingsRepository(session).get_user("theme", user.id) == "dark"
    assert SettingsRepository(session).get_system("theme") is None


def test_user_scope_values_are_independent_across_users(service, session, config):
    user_a = _make_user(session, role="user")
    user_b = _make_user(session, role="user")
    service.write_values({"theme": "dark"}, user_a)

    values_a = service.effective_values(user_a, include_providers=False)
    values_b = service.effective_values(user_b, include_providers=False)

    assert values_a["theme"] == "dark"
    # user_b non ha mai scritto nulla: la chiave è OMESSA (non un default
    # sostituito, e tantomeno l'override di user_a — nessuna cascata, a
    # differenza di 'system_with_user_override'). Vedi
    # test_user_with_no_row_omits_the_key_entirely per il motivo.
    assert "theme" not in values_b


def test_admin_writing_a_user_scoped_setting_writes_their_own_row_not_a_global_default(service, session):
    # A differenza di 'system_with_user_override', per scope='user' un admin
    # non ha un "default globale" speciale da scrivere: scrive sempre e solo
    # la propria riga, come qualunque altro utente.
    admin = _make_user(session, role="admin")

    service.write_values({"theme": "dark"}, admin)

    assert SettingsRepository(session).get_user("theme", admin.id) == "dark"
    assert SettingsRepository(session).get_system("theme") is None


def test_user_with_no_row_omits_the_key_entirely(service, session):
    # Fase 4b (revisione): a differenza di 'system_with_user_override', qui
    # non c'e' un fallback da restituire quando l'utente non ha mai scelto —
    # la chiave sparisce del tutto da effective_values, invece di essere
    # riempita col default dichiarato ("en"/"system"/False). L'assenza e'
    # l'unico modo per un consumer (vedi musizerr-ui PreferencesSync) di
    # distinguere "non ho mai scelto" da "ho scelto proprio il default": se
    # tornasse il default sostituito, le due situazioni sarebbero
    # indistinguibili e un client rischierebbe di sovrascrivere una scelta
    # reale che coincide col default.
    user = _make_user(session, role="user")

    values = service.effective_values(user, include_providers=False)

    assert "language" not in values
    assert "theme" not in values


def test_user_with_a_row_equal_to_the_default_is_still_present(service, session):
    # Contro-prova del test sopra: una volta che l'utente ha scritto una riga
    # (anche se il valore scritto coincide col default dichiarato), la chiave
    # torna a comparire — l'omissione riguarda solo l'assenza della riga, mai
    # il suo contenuto.
    user = _make_user(session, role="user")

    service.write_values({"theme": "system", "language": "en"}, user)
    values = service.effective_values(user, include_providers=False)

    assert values["theme"] == "system"
    assert values["language"] == "en"


@pytest.mark.parametrize("key,invalid_value", [("language", "fr"), ("theme", "purple")])
def test_enum_write_rejects_value_outside_declared_options(service, session, key, invalid_value):
    user = _make_user(session, role="user")

    with pytest.raises(SettingsWriteError) as exc_info:
        service.write_values({key: invalid_value}, user)

    assert exc_info.value.status_code == 400
    # Rifiutato: nessuna riga scritta.
    assert SettingsRepository(session).get_user(key, user.id) is None


# ---------------------------------------------------------------------------
# Utente sintetico "no-login" (routes/deps.py::_SYSTEM_USER, restituito da
# get_current_user per ogni richiesta quando `auth_enabled` e' False): e' il
# proprietario unico dell'istanza e la sua riga in `users` esiste
# (`user_service.ensure_system_user`), quindi le sue scelte personali
# (scope='user': lingua, tema, sfondo, unlike_on_origin_providers) si
# salvano per ``SYSTEM_USER_ID`` come quelle di qualunque utente.
# ---------------------------------------------------------------------------


def _system_user() -> User:
    # Costruito senza toccare il DB (a differenza di `_make_user`): il servizio
    # non lo legge mai da li', e' l'utente sintetico che le rotte consegnano senza
    # login.
    return User(id=SYSTEM_USER_ID, username="system", password_hash="", role="admin", is_active=True)


def test_synthetic_user_writes_a_user_scoped_setting_as_its_own(service, session, config):
    user_service.ensure_system_user(session)

    values = service.write_values({"theme": "dark", "language": "it"}, _system_user(), include_providers=False)

    assert (values["theme"], values["language"]) == ("dark", "it")
    assert SettingsRepository(session).get_user("theme", SYSTEM_USER_ID) == "dark"
    assert SettingsRepository(session).get_system("theme") is None  # never a global value


def test_synthetic_user_reads_back_only_what_it_chose(service, session):
    user_service.ensure_system_user(session)
    assert "theme" not in service.effective_values(_system_user(), include_providers=False)
    SettingsRepository(session).set_user("theme", SYSTEM_USER_ID, "dark")

    values = service.effective_values(_system_user(), include_providers=False)

    assert values["theme"] == "dark"
    assert "language" not in values  # never chosen: omitted, not substituted
    assert values["metadata_provider_priority"] == ["fakeprovider"]  # the effective order


def test_synthetic_user_does_not_see_another_users_choice(service, session):
    user_service.ensure_system_user(session)
    alice = _make_user(session, role="user")
    SettingsRepository(session).set_user("theme", alice.id, "dark")

    assert "theme" not in service.effective_values(_system_user(), include_providers=False)


def test_synthetic_user_can_still_edit_system_with_user_override_as_the_global_default(service, session, config):
    # A differenza di scope='user' puro, qui l'utente sintetico E' admin
    # (routes/deps.py::_SYSTEM_USER) e questa chiave HA un default globale
    # legittimo da editare — comportamento invariato, verificato qui per
    # escludere che il fix sopra l'abbia toccato per errore.
    system_user = _system_user()

    service.write_values({"metadata_provider_priority": ["a", "b"]}, system_user)

    assert SettingsRepository(session).get_system("metadata_provider_priority") == ["a", "b"]
    assert SettingsRepository(session).get_user("metadata_provider_priority", SYSTEM_USER_ID) is None
