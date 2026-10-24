"""
End-to-end regression of the deadlock identification_enabled <-> "provider
usable" (found in the review of phase 5c).

Before the fix, an identification provider's ``is_available()`` also looked at
``identification_enabled``. The requirement declared for turning
``identification_enabled`` on (``core/settings_catalog.py``) is "there is a
usable provider in the identification category": with the toggle off (the
default) NO provider was ever usable, so turning it on was structurally
impossible, whatever the library or the key.

The provider's half (``is_available()`` answers only "can I work?", from its
own settings) is tested in its own repository. This is the host's half, with a
generic identification provider that needs a setting of its own, as a real one
needs a key: the toggle can be turned on from a state where the toggle is off.
"""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import Session

from db.models import User
from musizeer_sdk.settings import ProviderSetting
from services.config_manager import ConfigManager
from services.settings import SettingsService

from tests.conftest import FakeIdentificationProvider, FakeRegistry


class _KeyedIdentification(FakeIdentificationProvider):
    """Needs a key to work, like a service identifying audio by fingerprint."""

    def __init__(self) -> None:
        super().__init__("keyed", available=True)

    def config_schema(self):
        return [ProviderSetting(key="api_key", type="string", label="API key", secret=True)]

    def validate_config(self, config: dict) -> bool:
        return bool(config.get("api_key"))


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def config(engine):
    return ConfigManager(engine=engine)


def _make_admin(session) -> User:
    user = User(username=f"admin-{uuid.uuid4()}", password_hash="x", role="admin")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_a_ready_provider_is_available_with_the_toggle_off():
    # The cause, directly: with the toggle off (the default) the provider must
    # still be available. Through the call protocol, as the host asks.
    from tests.loopback import as_proxy

    assert as_proxy(_KeyedIdentification()).is_available() is True


def test_write_enabling_identification_succeeds_when_provider_is_ready_even_though_toggle_starts_off(
    session, config,
):
    # The EXACT state of the deadlock: identification_enabled was never written
    # -> the default (False) is the effective value; the provider is ready.
    admin = _make_admin(session)
    provider = _KeyedIdentification()
    registry = FakeRegistry(identification=[provider])
    service = SettingsService(session, config, registry)

    # The provider's own settings are written through the same service, as the
    # user does from the UI, BEFORE turning identification_enabled on.
    service.write_values({f"provider.{provider.name}.api_key": "real-key"}, admin)

    assert config.get("identification_enabled") is not True  # not yet on: the deadlock's starting point

    # The write that, before the fix, ALWAYS failed (400, "unavailable --
    # requires a usable 'identification' provider"): no sequence of writes
    # could unblock it.
    result = service.write_values({"identification_enabled": True}, admin)

    assert result["identification_enabled"] is True
