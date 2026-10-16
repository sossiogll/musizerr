"""POST /auth/{provider}/test (routes/auth.py::test_connection) e la
definizione condivisa di "usable" (core.settings_resolver.compute_provider_usable),
esercitate dal lato route — vedi anche test_config_route.py per
find_provider_by_name in isolamento e per "usable" su un provider flow=NONE.

Regola d'oro rispettata: nessun nome di provider concreto reale qui, solo
fake locali (`_OAuthMetadataProvider`, FakeIdentificationProvider).
"""

from __future__ import annotations

import time

import pytest
from sqlmodel import Session

from core.auth import AccountTarget
from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow, BearerHeaderRequestAuth, OAuth2Config
from services.config_manager import ConfigManager
from db.auth_store import CredentialStore
from db.models import User
from routes.auth import test_connection as auth_test_connection
from routes.config import list_providers

from tests.conftest import FakeIdentificationProvider, FakeMetadataProvider, FakeRegistry


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def config(engine):
    return ConfigManager(engine=engine)


def _make_user(session, *, role: str) -> User:
    user = User(username=f"{role}-user", password_hash="x", role=role)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# POST /auth/{provider}/test: deve trovare un provider in OGNI categoria che
# il registry dichiara — prima del fix, routes/auth.py::_get_provider era una
# terza copia scritta a mano della stessa catena di 5 categorie che ometteva
# "identification" (le altre due, in routes/config.py, erano già state
# corrette derivandole da PROVIDER_CATEGORY_GETTERS). Con la chiave di un
# provider di identificazione configurata e valida, il test falliva comunque
# con 404 "Provider ... not found": qui riprodotto con un fake della stessa categoria.
# ---------------------------------------------------------------------------


def test_test_connection_finds_a_provider_in_every_category_the_registry_declares(session, config):
    admin = _make_user(session, role="admin")
    registry = FakeRegistry(identification=[FakeIdentificationProvider("fakeident")])

    # Non deve sollevare HTTPException(404, "Provider 'fakeident' not found")
    # — prima del fix, _get_provider non guardava affatto in "identification".
    result = auth_test_connection("fakeident", registry, session, admin, scope="account")

    assert result["account"] == {"ok": True, "detail": "Connected"}


# ---------------------------------------------------------------------------
# "usable" per un provider OAuth2: deve essere True quando App (client OAuth2
# nel credential store) e Account (token OAuth2) sono entrambi connessi.
# Prima del fix, validate_config di un provider OAuth2 cercava client_id/
# client_secret nella CONFIG (settings), che questi provider non leggono mai a
# runtime per le credenziali OAuth2 (lette invece dal credential store via
# AuthManager.require_oauth2_client) — quindi config_valid, e con esso
# usable, restava False anche ad App+Account connessi. Qui config_valid deve
# essere True a prescindere (nessun campo required nella config del fake),
# cosi' usable segue solo authenticated.
# ---------------------------------------------------------------------------


class _OAuthMetadataProvider(FakeMetadataProvider):
    """Metadata provider OAuth2 finto: nessun campo required nella propria
    config (come un provider OAuth2 reale dopo il fix — client_id/client_secret non
    vivono più in config_schema())."""

    auth = AuthDefinition(
        flow=AuthFlow.OAUTH2,
        account_model=AccountModel.PERSONAL_WITH_SHARED,
        oauth2=OAuth2Config(
            authorize_url="https://example.invalid/authorize",
            token_url="https://example.invalid/token",
            scopes=[],
            request_auth=BearerHeaderRequestAuth(),
        ),
    )

    def validate_config(self, config: dict) -> bool:
        return True


def test_usable_is_true_for_oauth_provider_with_app_and_account_connected(session, config):
    admin = _make_user(session, role="admin")
    provider = _OAuthMetadataProvider("fakeoauth")
    registry = FakeRegistry(metadata=[provider])

    # App: client OAuth2 salvato (sezione "App" della card).
    store = CredentialStore(session, user_id=None)
    store.save_oauth2_client("fakeoauth", "client-id", "client-secret")
    # Account: token OAuth2 valido e non scaduto (sezione "Account").
    store.save_oauth2_token(
        "fakeoauth",
        {"access_token": "tok", "obtained_at": time.time(), "expires_in": 3600},
        AccountTarget.SHARED,
    )
    session.commit()

    data = list_providers(registry, config, session, admin)
    info = next(p for p in data["metadata"] if p["name"] == "fakeoauth")

    assert info["authenticated"] is True
    assert info["config_valid"] is True
    assert info["usable"] is True
