"""
``services.identification.identify_confidently``: la soglia di confidenza
generica (``identification_confidence_threshold``) è applicata dall'host
sull'identità restituita dal provider.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from musizerr_sdk.interfaces import Identity, WorkspaceFile
from services.identification import identify_confidently


class _Provider:
    def __init__(self, identity):
        self._identity = identity
        self.calls = 0

    def identify(self, audio):
        self.calls += 1
        return self._identity


def _config(threshold: float) -> SimpleNamespace:
    return SimpleNamespace(identification_confidence_threshold=threshold)


def _identity(score: float) -> Identity:
    return Identity(title="Song", artist="Artist", score=score, recording_id="rid")


@pytest.mark.parametrize("score, confident", [(0.89, False), (0.90, True), (0.95, True)])
def test_threshold_is_applied_by_the_host(score, confident):
    provider = _Provider(_identity(score))

    result = identify_confidently(provider, WorkspaceFile(workspace="/tmp", name="a.mp3"), _config(0.90))

    assert (result is not None) is confident
    assert provider.calls == 1


def test_no_match_stays_none():
    assert identify_confidently(_Provider(None), WorkspaceFile(workspace="/tmp", name="a.mp3"), _config(0.5)) is None
