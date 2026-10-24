"""
The host's side of the sources integration test (scripts/test-sources-integration.sh): what the
application's own registry sees of the gateway after the CLI installed or uninstalled a provider.

    python sources_host_check.py lyrics    # lrclib is registered and gives lyrics (through the gateway)
    python sources_host_check.py absent    # lrclib is not registered
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("DATA_DIR", "/tmp/data")

from musizeer_sdk.auth import AuthSession  # noqa: E402
from musizeer_sdk.models import Track  # noqa: E402
from services.provider_registry import ProviderRegistry  # noqa: E402


def main(mode: str) -> int:
    registry = ProviderRegistry()
    lrclib = registry.get_lyrics_provider("lrclib")
    if mode == "absent":
        if lrclib is not None:
            print("FAILED: lrclib is still registered", flush=True)
            return 1
        print("ABSENT_OK", registry.lyrics_names(), flush=True)
        return 0
    if lrclib is None:
        print(f"FAILED: lrclib is not registered: {registry.lyrics_names()} {registry.rejected()}", flush=True)
        return 1
    lyrics = lrclib.get_lyrics(
        Track(title="Bohemian Rhapsody", artist="Queen", album="A Night at the Opera", duration_ms=354_000), AuthSession.anonymous(),
    )
    if lyrics is None or lyrics.source != "lrclib" or not lyrics.synced or "real life" not in (lyrics.plain or ""):
        print(f"FAILED: no lyrics came back: {lyrics!r}", flush=True)
        return 1
    print("LYRICS_OK", lyrics.source, len(lyrics.plain or ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
