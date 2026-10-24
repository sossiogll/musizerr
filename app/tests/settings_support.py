"""
A registry with one provider of every category, each declaring settings, for
the tests of how the host turns what providers declare (``config_schema()``)
into its own settings. The providers are generic fakes (nobody's product): one
of them fills two roles (metadata and sync), the case the host must not count
twice.
"""
from __future__ import annotations

from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizeer_sdk.interfaces import LyricsProvider, MetadataProvider
from musizeer_sdk.models import Lyrics
from musizeer_sdk.settings import ProviderSetting
from services.provider_registry import ProviderRegistry

from tests.conftest import (
    FakeDownloadProvider,
    FakeIdentificationProvider,
    FakeMetadataProvider,
    FakeStorageProvider,
    FakeSyncProvider,
)
from tests.loopback import as_proxy

NONE = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)


def _schema(mixin_name: str) -> list[ProviderSetting]:
    return [
        ProviderSetting(key="url", type="string", label=f"{mixin_name} URL", default=""),
        ProviderSetting(key="mode", type="enum", label="Mode", default="full", options=("full", "meta")),
        ProviderSetting(key="limit", type="int", label="Limit", default=20, minimum=1, maximum=100, step=1),
        ProviderSetting(key="secret", type="string", label="Secret", secret=True, required=False,
                        visible_when=(("mode", "full"),)),
    ]


class SettingsMetadata(FakeMetadataProvider):
    def config_schema(self):
        return _schema(self.name)


class SettingsDual(FakeSyncProvider, MetadataProvider):
    """A sync provider that is also a metadata provider: one object, two roles."""

    def __init__(self, name: str = "dual") -> None:
        FakeSyncProvider.__init__(self, name)

    def config_schema(self):
        return _schema(self.name)


class SettingsDownload(FakeDownloadProvider):
    def config_schema(self):
        return _schema(self.name)


class SettingsStorage(FakeStorageProvider):
    def config_schema(self):
        return _schema(self.name)


class SettingsIdentification(FakeIdentificationProvider):
    def config_schema(self):
        return _schema(self.name)


class SettingsLyrics(LyricsProvider):
    auth = NONE

    def __init__(self, name: str = "words") -> None:
        from musizeer_sdk.context import StaticProviderContext

        super().__init__(StaticProviderContext())
        self.name = self.label = name

    def config_schema(self):
        return _schema(self.name)

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_lyrics(self, track, session):
        return Lyrics(plain="")


def registry_of_every_category() -> ProviderRegistry:
    """The real ``ProviderRegistry`` holding one provider per category (and the
    dual one in two)."""
    registry = ProviderRegistry(discover=False)
    for provider in (
        SettingsMetadata("catalog"), SettingsDual("dual"), SettingsDownload("fetch"), SettingsStorage("shelf"),
        SettingsIdentification("ident"), SettingsLyrics("words"),
    ):
        registry.register(as_proxy(provider))
    return registry
