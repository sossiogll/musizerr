"""
The host encodes every call for the SDK the provider was built on (ADR-0007 §3.1): a provider built on an
older SDK, which decodes strictly, never meets a field it would not know; one built before 5.5, whose
manifest does not say its SDK, is treated as 5.4. A contract type that gained a field in 5.5 is stood for
by ``HostEvolutionProbe``.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

import pytest

from musizerr_sdk import manifest as manifest_module, protocol, wire
from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.interfaces import PROVIDER_API_VERSION, LyricsProvider
from musizerr_sdk.version import SDK_VERSION
from services.provider_proxy import connect


@wire.register
@dataclass(frozen=True)
class HostEvolutionProbe:
    title: str
    mood: str | None = field(default=None, metadata={wire.SINCE: "5.5"})


class Lyrics(LyricsProvider):
    name = "lyrics"
    label = "Lyrics"
    provider_api_version = PROVIDER_API_VERSION
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_lyrics(self, track, session):
        return None


class Recording:
    """Answers the describe with a manifest that says ``sdk_version``, records every call after it."""

    def __init__(self, sdk_version: str | None) -> None:
        built = manifest_module.build(Lyrics(StaticProviderContext()))
        self.manifest = dataclasses.replace(built, sdk_version=sdk_version)
        self.calls: list[dict] = []

    def request(self, request):
        message = protocol.load_message(request)
        if message["op"] == protocol.OP_DESCRIBE:
            return iter([protocol.manifest_frame(self.manifest, peer=None)])
        self.calls.append(message)
        return iter([protocol.result_frame(None)])


@pytest.mark.parametrize("sdk_version, sent", [
    (None, {"title"}),          # a provider built before 5.5
    ("5.4.0", {"title"}),
    ("5.5.0", {"title", "mood"}),
    ("5.9.1", {"title", "mood"}),
])
def test_a_call_is_encoded_for_the_providers_sdk(sdk_version, sent):
    transport = Recording(sdk_version)
    proxy = connect(transport, StaticProviderContext())

    proxy.get_lyrics(HostEvolutionProbe("Song", mood="calm"), AuthSession.anonymous())

    (call,) = transport.calls
    assert call["sdk"] == SDK_VERSION
    assert set(call["args"][0]) - {"$"} == sent
