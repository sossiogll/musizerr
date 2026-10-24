"""
The host's side of the gateway integration test (scripts/test-gateway-integration.sh).

Runs in a container of the application image, with the gateway's socket volume and the library volume
mounted, exactly where the application and its workers have them. It uses the application's own code:
``ProviderRegistry``, ``ProviderReloader``, ``GatewayWatcher``, ``HostProviderContext`` and the settings
table, and checks:

  a. lyrics come back from the LRCLIB image, through the gateway, via the registry;
  b. a provider that was installed after the registry was built reaches it through the
     providers-changed event on Redis;
  c. a hostile provider's every attempt to get out of its sandbox fails (and its allowed
     controls work);
  d. what a setting names is reachable by the provider while it is set, and not after.

It prints marker lines the script greps for, and exits non-zero on the first thing that is wrong.
"""
from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("DATA_DIR", "/tmp/data")

from db.engine import engine  # noqa: E402
from services.database_init import init_db  # noqa: E402
from sqlmodel import Session  # noqa: E402

from core.settings_resolver import namespace_provider_key  # noqa: E402
from db.settings_store import SettingsRepository  # noqa: E402
from musizeer_sdk.auth import AuthSession  # noqa: E402
from musizeer_sdk.models import Track  # noqa: E402
from services import provider_events  # noqa: E402
from services.config_manager import get_config  # noqa: E402
from services.provider_registry import ProviderRegistry  # noqa: E402

SESSION = AuthSession.anonymous()


def say(marker: str, **fields) -> None:
    print(marker, json.dumps(fields) if fields else "", flush=True)


def fail(message: str) -> None:
    print("FAILED:", message, flush=True)
    sys.exit(1)


def lyrics_of(provider, title: str, artist: str = "Queen", album: str = "A Night at the Opera", duration_ms: int = 354_000):
    return provider.get_lyrics(Track(title=title, artist=artist, album=album, duration_ms=duration_ms), SESSION)


def set_setting(provider: str, key: str, value: str) -> None:
    with Session(engine) as session:
        SettingsRepository(session).set_system(namespace_provider_key(provider, key), value)
        session.commit()


def main() -> None:
    init_db()
    registry = ProviderRegistry()
    say("REGISTRY", lyrics=registry.lyrics_names(), rejected=registry.rejected(), gateway_key=registry.gateway_key)

    lrclib = registry.get_lyrics_provider("lrclib")
    if lrclib is None:
        fail(f"lrclib is not in the registry: {registry.lyrics_names()} {registry.rejected()}")
    lyrics = lyrics_of(lrclib, "Bohemian Rhapsody")
    if lyrics is None or lyrics.source != "lrclib" or not lyrics.synced or "real life" not in (lyrics.plain or ""):
        fail(f"no lyrics came back from the LRCLIB image: {lyrics!r}")
    say("LYRICS_OK", source=lyrics.source, synced_lines=len(lyrics.synced), first_line=lyrics.plain.splitlines()[0])

    provider_events.ProviderReloader(registry).start()
    provider_events.GatewayWatcher(registry, interval=1.0).start()
    say("READY")

    deadline = time.monotonic() + 180
    while registry.get_lyrics_provider("hostile") is None:
        if time.monotonic() > deadline:
            fail(f"the provider installed later never reached the registry: {registry.lyrics_names()} {registry.rejected()}")
        time.sleep(0.5)
    say("RELOADED", lyrics=registry.lyrics_names(), gateway_key=registry.gateway_key)

    hostile = registry.get_lyrics_provider("hostile")
    report = json.loads(lyrics_of(hostile, "attack").plain)
    for item in report:
        print(("  ok   " if item["ok"] else "  FAIL ") + ("[control] " if item["control"] else "[attack]  ") + item["name"] + " -- " + item["detail"], flush=True)
    wrong = [item["name"] for item in report if not item["ok"]]
    if wrong:
        fail(f"{len(wrong)} of {len(report)} checks of the hostile provider went wrong: {wrong}")
    say("HOSTILE_OK", checks=len(report), attacks=sum(1 for i in report if not i["control"]), controls=sum(1 for i in report if i["control"]))

    # d. a destination a setting names is reachable while the setting holds it
    before = lyrics_of(hostile, "connect:example.com:443").plain
    set_setting("hostile", "server_url", "https://example.com")
    get_config()._cache_loaded_at = 0.0  # noqa: SLF001 — read the settings now, not after the cache window
    during = lyrics_of(hostile, "connect:example.com:443").plain
    set_setting("hostile", "server_url", "")
    after = lyrics_of(hostile, "connect:example.com:443").plain
    if not (" 403 " in before and " 200 " in during and " 403 " in after):
        fail(f"the grant did not follow the setting: before={before!r} during={during!r} after={after!r}")
    say("GRANT_OK", before=before, during=during, after=after)


if __name__ == "__main__":
    main()
