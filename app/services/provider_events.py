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
  a gateway that restarted knows no installed provider until the host tells it, and the
  watcher is what notices (the ``desired`` identifier of its listing is not the database's)
  and pushes the set again.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable

from services.config_manager import get_config
from services.gateway_client import GatewayClient, GatewayUnreachable
from services.gateway_sync import GatewaySync

logger = logging.getLogger(__name__)

CHANNEL = "musizerr:providers"
WATCH_INTERVAL_SECONDS = 5.0
COALESCE_SECONDS = 0.3
RECONNECT_SECONDS = 2.0


def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://redis:6379/0")


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
                 workspace_dir: Callable[[], object] | None = None) -> None:
        self._registry = registry
        self._client = client or GatewayClient()
        self._sync = sync
        self._announce = announce
        self._interval = interval
        # The host's workspace folder (the setting), compared with the gateway's own (docs/deployment.md).
        self._workspace_dir = workspace_dir or (lambda: get_config().workspace_dir)
        self._workspace_warned: tuple[str, str] | None = None
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

    def _check_workspace(self, gateway_root: str) -> None:
        """The host and the gateway must name the same workspace folder: the gateway mounts
        ``<folder>/<provider>`` in each sandbox and the host creates its call folders in ITS folder,
        so with two different ones every call that carries a file fails, far from the cause. Said once
        per pair of values, in the log (the setting and ``GATEWAY_WORKSPACE_DIR`` come from ``WORKSPACE_DIR``)."""
        try:
            mine = os.path.normpath(str(self._workspace_dir()))
        except Exception:  # noqa: BLE001 — the settings may not be readable yet; the next check looks again
            return
        theirs = os.path.normpath(gateway_root)
        if mine == theirs:
            self._workspace_warned = None
            return
        if self._workspace_warned != (mine, theirs):
            self._workspace_warned = (mine, theirs)
            logger.error(
                "The workspace folder of the host (setting workspace_dir: %s) is not the gateway's (GATEWAY_WORKSPACE_DIR: %s): "
                "providers cannot exchange files with Musizerr. Set both from WORKSPACE_DIR, or change the setting to the gateway's "
                "value (docs/deployment.md).", mine, theirs,
            )

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.check()
