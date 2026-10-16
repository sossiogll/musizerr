"""
What a failing download provider means to the host (the contract of
``DownloadProvider``): ``None`` is the definitive "I don't have it" (never
retried), a raised error is "I could not ask" (the host tries again later).

Real case (29 Sep, first run of the transient-failure routine): "Control"
failed definitively in 0.45 s because two providers turned a refused
connection and a 503 into ``None``. Each provider is tested against its own
upstream in its own repository; here the host's reading of the two answers.
"""
from __future__ import annotations

import pytest

from core.download_resolver import DownloadResolver
from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.identifiers import PROVIDER_ID, Identifier

from tests.conftest import FakeAuthManager, FakeDownloadProvider, FakeRegistry, make_track


class _Provider(FakeDownloadProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    supported_identifier_types = {"dl_id"}
    supports_fuzzy = True

    def __init__(self, outcome) -> None:
        super().__init__("dl")
        self.outcome = outcome
        self.calls: list[str] = []

    def _answer(self):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def download_by_id(self, identifier, workspace, session):
        self.calls.append("id")
        return self._answer()

    def download_fuzzy(self, track, workspace, session):
        self.calls.append("fuzzy")
        return self._answer()


def _resolve(provider, tmp_path, *, with_id: bool):
    track = make_track(title="Control")
    if with_id:
        track.add_id(Identifier(type=PROVIDER_ID, value="x1", provider="dl"))
    resolver = DownloadResolver(FakeRegistry(downloads=[provider]), FakeAuthManager(), ["dl"])
    return resolver.resolve(track, lambda name: str(tmp_path))


@pytest.mark.parametrize("with_id", [True, False], ids=["by id", "fuzzy"])
@pytest.mark.parametrize("error", [
    ConnectionRefusedError(111, "Connection refused"),
    TimeoutError("timed out"),
    RuntimeError("upstream answered 503"),
])
def test_a_provider_that_raises_is_retried_later(tmp_path, error, with_id):
    provider = _Provider(error)

    result = _resolve(provider, tmp_path, with_id=with_id)

    assert provider.calls == ["id" if with_id else "fuzzy"]
    assert result.success is False
    assert result.retriable is True


@pytest.mark.parametrize("with_id", [True, False], ids=["by id", "fuzzy"])
def test_a_provider_that_answers_none_is_a_definitive_no(tmp_path, with_id):
    provider = _Provider(None)

    result = _resolve(provider, tmp_path, with_id=with_id)

    assert provider.calls == ["id" if with_id else "fuzzy"]
    assert result.success is False
    assert result.retriable is False
