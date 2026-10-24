"""
"The providers changed": how every process rebuilds its registry.

The set of providers changes in ONE place (the gateway starts and stops provider processes; an
install or removal changes what it runs) but is used in four processes: the API and the three
workers. Each holds its own :class:`ProviderRegistry`, so a change is announced on a Redis
channel and every process rebuilds its registry when it hears it. Only the host talks to Redis:
the gateway never does.

- :func:`announce_changed` publishes the event (after an install, or when the gateway's set
  of running providers changed);
- :class:`ProviderReloader` runs in every process that holds a registry: it listens, and
  rebuilds the registry (``ProviderRegistry.reload``) on each event. After a lost connection it
  rebuilds once more, since an event may have been missed meanwhile, and on its first subscription
  it does when the gateway's set already differs from what the registry loaded. A worker starts
  one per PROCESS that runs tasks (``services/process_registry.py``), never in the parent that forks;
- :class:`GatewayWatcher` runs in the API only. It compares the gateway's ``epoch`` and
  ``generation`` (they change whenever its set of running providers does, a restart
  included) with what the registry last loaded, and announces the difference: it is how a
  provider that came up after the host started, or one that crashed, reaches every process.
  It also keeps the gateway's DESIRED set equal to the database's (``services/gateway_sync.py``):
  a gateway that restarted knows no installed provider, root folder or working folder until the host tells it, and the
  watcher is what notices (the ``desired`` identifier of its listing is not the database's)
  and pushes the set again. The first time the gateway answers it also runs the one-shot conversion of the folder
  values an older version stored (``services/roots.convert_legacy``, ADR-0008), and after every check compares the
  working folder it holds with its own, a safety net that only logs.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable

from core import environment
from services.config_manager import get_config
from services.gateway_client import GatewayClient, GatewayUnreachable
from services.gateway_sync import GatewaySync
from services import roots as roots_service

logger = logging.getLogger(__name__)

CHANNEL = "musizeer:providers"
WATCH_INTERVAL_SECONDS = 5.0
COALESCE_SECONDS = 0.3
RECONNECT_SECONDS = 2.0


def redis_url() -> str:
    return environment.redis_url()


def announce_changed() -> None:
    """Tell every process that the set of providers changed."""
    import redis

    client = redis.from_url(redis_url())
    try:
        client.publish(CHANNEL, "changed")
    finally:
        client.close()


class ProviderReloader:
    """Rebuilds ``registry`` whenever the providers-changed event arrives, then calls ``after_reload``
    (the API seals the secret settings the providers now declare: ``services/secret_settings.py``)."""

    def __init__(
        self, registry, connect: Callable[[], object] | None = None, after_reload: Callable[[], None] | None = None,
    ) -> None:
        self._registry = registry
        self._connect = connect or self._redis_pubsub
        self._after_reload = after_reload
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="provider-reloader", daemon=True)
        self.reloads = 0

    @staticmethod
    def _redis_pubsub():
        import redis

        pubsub = redis.from_url(redis_url()).pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(CHANNEL)
        return pubsub

    def start(self) -> "ProviderReloader":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)

    def _is_stale(self) -> bool:
        try:
            return self._registry.is_stale()
        except Exception:  # noqa: BLE001 — the listener must survive; the next event rebuilds anyway
            logger.exception("could not compare the provider registry with the gateway")
            return False

    def _reload(self) -> None:
        try:
            self._registry.reload()
            self.reloads += 1
        except Exception:  # noqa: BLE001 — a failed rebuild keeps the registry it has
            logger.exception("the provider registry could not be rebuilt")
            return
        if self._after_reload is not None:
            try:
                self._after_reload()
            except Exception:  # noqa: BLE001 — what follows a rebuild never takes the listener down
                logger.exception("what follows a rebuild of the provider registry failed")

    def _run(self) -> None:
        reconnected = False
        while not self._stop.is_set():
            try:
                pubsub = self._connect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("provider events: cannot subscribe (%s); retrying", exc)
                reconnected = True
                if self._stop.wait(RECONNECT_SECONDS):
                    return
                continue
            # An event may have been missed while it was down, or before this first subscription
            # (a registry built just before it): the gateway's set is then not the one it loaded.
            if reconnected or self._is_stale():
                self._reload()
            try:
                while not self._stop.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if message is None:
                        continue
                    while pubsub.get_message(timeout=COALESCE_SECONDS) is not None:
                        pass  # a burst of events is one rebuild
                    self._reload()
            except Exception as exc:  # noqa: BLE001
                logger.warning("provider events: connection lost (%s); resubscribing", exc)
                reconnected = True
            finally:
                try:
                    pubsub.close()
                except Exception:  # noqa: BLE001
                    pass
                if not self._stop.is_set():
                    self._stop.wait(RECONNECT_SECONDS)


class GatewayWatcher:
    """Announces a change of the gateway's set of running providers."""

    def __init__(self, registry, client: GatewayClient | None = None, announce: Callable[[], None] = announce_changed,
                 interval: float = WATCH_INTERVAL_SECONDS, sync: GatewaySync | None = None,
                 workspace_dir: Callable[[], object] | None = None,
                 convert: Callable[[], object] | None = None) -> None:
        self._registry = registry
        self._client = client or GatewayClient()
        self._sync = sync
        self._announce = announce
        self._interval = interval
        # The host's working folder (the setting), compared with the one the gateway holds: a safety net, since the host
        # tells the gateway (ADR-0008). A callable that raises (the folder is not set) compares nothing.
        self._workspace_dir = workspace_dir or (lambda: get_config().workspace_dir)
        self._workspace_warned: tuple[str, str] | None = None
        # The conversion of today's folder values, run while there is something to convert.
        self._convert = convert if convert is not None else self._convert_legacy
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="gateway-watcher", daemon=True)

    def start(self) -> "GatewayWatcher":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)

    def check(self) -> bool:
        """Announce if the gateway's set differs from what the registry loaded; True if it did."""
        try:
            listing = self._client.listing()
        except GatewayUnreachable:
            return False
        self._run_conversion()
        self._check_workspace(listing.workspace_root)
        if self._sync is not None:
            try:
                self._sync.ensure(listing)  # the next check sees the providers it starts
            except Exception as exc:  # noqa: BLE001 — the database may be busy: the next check tries again
                logger.warning("could not keep the gateway's providers in line with the database: %s", exc)
        if (listing.epoch, listing.generation) == self._registry.gateway_key:
            return False
        try:
            self._announce()
        except Exception as exc:  # noqa: BLE001 — Redis down: the next check tries again
            logger.warning("could not announce a change of the providers: %s", exc)
            return False
        return True

    def _convert_legacy(self) -> object:
        """Convert the folder values an older version stored (ADR-0008), once the gateway answers; idempotent."""
        from sqlmodel import Session

        from db.engine import engine

        with Session(engine) as session:
            if not roots_service.pending(session, self._registry):
                return None
            return roots_service.convert_legacy(
                session, self._client, self._registry, on_written=lambda written: get_config().committed(
                    {key: value for key, value in written.items() if key == "workspace_dir"}
                ),
            )

    def _run_conversion(self) -> None:
        try:
            converted = self._convert()
        except Exception as exc:  # noqa: BLE001 — the database may be busy, the registry empty: the next check tries again
            logger.warning("could not convert the folder values of an older version: %s", exc)
            return
        if converted is not None and self._sync is not None:
            try:
                self._sync.push()  # the gateway learns the roots and the working folder the conversion made
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not tell the gateway the folders the conversion made: %s", exc)

    def _check_workspace(self, gateway_root: str | None) -> None:
        """Safety net, not a mechanism: the host tells the gateway its working folder (ADR-0008), so the two cannot drift. After
        a push the two are compared anyway; if they differ (a root the gateway refused, a push that did not arrive) it is
        said once per pair of values, in the log. Nothing is compared while either folder is not set."""
        try:
            mine = os.path.normpath(str(self._workspace_dir()))
        except Exception:  # noqa: BLE001 — not set (or the settings are not readable yet): the next check looks again
            self._workspace_warned = None
            return
        if gateway_root is None:
            return  # the gateway has none (yet): the push, or its verdict on the root, says why
        theirs = os.path.normpath(gateway_root)
        if mine == theirs:
            self._workspace_warned = None
            return
        if self._workspace_warned != (mine, theirs):
            self._workspace_warned = (mine, theirs)
            logger.error(
                "The working folder of the host (%s) is not the one the gateway holds (%s): providers cannot exchange files "
                "with Musizeer until the host pushes it again (Administration > Root folders shows the gateway's verdict).",
                mine, theirs,
            )

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.check()
