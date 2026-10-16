"""
``StaticProviderContext`` (``musizerr_sdk.context``) is how a provider is tested
without the host: its timeouts default to the very values the host's settings
catalog gives an untouched installation, because the catalog takes them from
the SDK (one source).
"""
from __future__ import annotations

import pytest

from core.settings_catalog import get_def
from musizerr_sdk.context import StaticProviderContext


@pytest.mark.parametrize("key", ["http_timeout", "download_timeout", "connect_timeout", "stream_start_timeout"])
def test_a_timeout_defaults_to_the_default_of_the_settings_catalog(key):
    assert getattr(StaticProviderContext(), key) == get_def(key).default


def test_a_timeout_given_to_the_context_wins():
    context = StaticProviderContext(http_timeout=1, download_timeout=2, connect_timeout=3, stream_start_timeout=4)

    assert (context.http_timeout, context.download_timeout, context.connect_timeout, context.stream_start_timeout) == (
        1, 2, 3, 4,
    )


def test_the_public_urls_carry_the_prefix_and_the_provider_of_the_context():
    context = StaticProviderContext(provider_name="some provider", api_prefix="/music/api")

    assert context.cover_url("c 1") == "/music/api/cover/provider/some%20provider/c%201"
    assert context.stream_url("s1") == "/music/api/stream/provider/some%20provider/s1"
    assert context.preview_url("p1") == "/music/api/preview/some%20provider/p1"
