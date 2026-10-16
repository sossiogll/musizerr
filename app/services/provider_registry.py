"""
Host: registers the providers the gateway runs — it is not itself a plugin,
which is why it lives here and not in a package of providers.

The host never holds a provider instance and no provider code lives in this
repository. It registers proxies (``services/provider_proxy.py``) under the
roles the provider's manifest declares, and every call goes through the codec
and the call protocol. The providers come from the ``provider-gateway``, which
runs each of them in its own sandbox: the list is given by its control socket,
the manifest by the provider itself (``describe``) and the transport is its
socket (``services/provider_socket.py``). If the gateway does not answer the
registry starts without providers (and says so in the log), never a crash.
"""
from __future__ import annotations
import logging

from musizerr_sdk.interfaces import (
    PROVIDER_API_VERSION,
    DownloadProvider,
    IdentificationProvider,
    LyricsProvider,
    MetadataProvider,
    StorageProvider,
    SyncProvider,
)
from musizerr_sdk.manifest import ROLES
from services.config_manager import get_config
from services.gateway_client import (
    EgressGrants,
    GatewayClient,
    GatewayProvider,
    GatewayProviderContext,
    GatewayUnreachable,
    parse_permissions,
)
from services.provider_proxy import ProviderCallFailed, ProviderProxy, connect
from services.provider_socket import SocketTransport
from services.workspace import PROVIDER_NAME

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """
    The providers the gateway runs, by role. A provider is registered under
    EVERY role its manifest declares (a sync provider that is also a metadata
    provider appears in both).
    """

    def __init__(
        self, *, discover: bool = True, gateway: GatewayClient | None = None, grants: EgressGrants | None = None,
    ) -> None:
        self._by_role: dict[str, dict[str, ProviderProxy]] = {role: {} for role in ROLES}
        self._rejected: dict[str, str] = {}
        self._gateway = gateway or GatewayClient()
        self._grants = grants or EgressGrants(self._gateway)
        # (epoch, generation) of the gateway when this registry last asked it: what the
        # GatewayWatcher compares the gateway's current state with.
        self.gateway_key: tuple[str, int] | None = None
        if discover:
            self._load_gateway()

    # ------------------------------------------------------------------
    # Access to the providers
    # ------------------------------------------------------------------

    def get_metadata_provider(self, name: str) -> MetadataProvider | None:
        return self._by_role["metadata"].get(name)

    def get_download_provider(self, name: str) -> DownloadProvider | None:
        return self._by_role["download"].get(name)

    def get_lyrics_provider(self, name: str) -> LyricsProvider | None:
        return self._by_role["lyrics"].get(name)

    def get_storage_provider(self, name: str) -> StorageProvider | None:
        return self._by_role["storage"].get(name)

    def get_sync_provider(self, name: str) -> SyncProvider | None:
        return self._by_role["sync"].get(name)

    def get_identification_provider(self, name: str) -> IdentificationProvider | None:
        return self._by_role["identification"].get(name)

    def all_metadata(self) -> list[MetadataProvider]:
        return list(self._by_role["metadata"].values())

    def all_download(self) -> list[DownloadProvider]:
        return list(self._by_role["download"].values())

    def all_lyrics(self) -> list[LyricsProvider]:
        return list(self._by_role["lyrics"].values())

    def all_storage(self) -> list[StorageProvider]:
        return list(self._by_role["storage"].values())

    def all_sync(self) -> list[SyncProvider]:
        return list(self._by_role["sync"].values())

    def all_identification(self) -> list[IdentificationProvider]:
        return list(self._by_role["identification"].values())

    def metadata_names(self) -> list[str]:
        return list(self._by_role["metadata"])

    def download_names(self) -> list[str]:
        return list(self._by_role["download"])

    def lyrics_names(self) -> list[str]:
        return list(self._by_role["lyrics"])

    def storage_names(self) -> list[str]:
        return list(self._by_role["storage"])

    def sync_names(self) -> list[str]:
        return list(self._by_role["sync"])

    def identification_names(self) -> list[str]:
        return list(self._by_role["identification"])

    def names_for_category(self, category: str) -> list[str]:
        """Lookup generico per categoria (usato per validare un provider_ref
        senza che il chiamante conosca l'elenco fisso dei metodi *_names).
        Data-driven: nessuna catena if/elif, categoria sconosciuta -> lista
        vuota."""
        return list(self._by_role.get(category, ()))

    # ------------------------------------------------------------------
    # The providers of the gateway
    # ------------------------------------------------------------------

    def rejected(self) -> dict[str, str]:
        """The providers of the gateway that the host refuses, with the reason."""
        return dict(self._rejected)

    def reload(self) -> None:
        """Rebuild this process's registry from the providers the gateway runs
        now. Called by the process itself when it hears that the providers changed
        (``services/provider_events.py``), never by the code that changed them. The new registry is
        built apart and swapped in, so a reader never sees a half-built one and a failed rebuild
        leaves the old one."""
        fresh = ProviderRegistry(gateway=self._gateway, grants=self._grants)
        self._by_role, self._rejected, self.gateway_key = fresh._by_role, fresh._rejected, fresh.gateway_key

    def register(self, proxy: ProviderProxy) -> None:
        """Register ``proxy`` under every role of its manifest."""
        for role in proxy.manifest.roles:
            self._by_role[role][proxy.name] = proxy
        logger.debug("Registered provider: %s (%s)", proxy.name, ", ".join(proxy.manifest.roles))

    def _load_gateway(self) -> None:
        """Register the providers the gateway runs. They are described through their own socket; the
        gateway only says which are running and what their images declare (which is checked
        against what the provider then says of itself)."""
        try:
            listing = self._gateway.listing()
        except GatewayUnreachable as exc:
            log = logger.warning if self._gateway.deployed() else logger.info
            log("Starting without the providers of the gateway: %s", exc)
            return
        self.gateway_key = (listing.epoch, listing.generation)
        for item in listing.providers:
            if item.state != "running":
                logger.info("Gateway provider %s is %s%s", item.name, item.state, f": {item.reason}" if item.reason else "")
                continue
            try:
                self._register_from_gateway(item)
            except Exception as exc:  # noqa: BLE001 — one provider must not take the registry down
                self._rejected[item.name] = f"cannot be used: {exc}"
                logger.warning("Refused gateway provider %s: %s", item.name, exc)

    def _register_from_gateway(self, item: GatewayProvider) -> None:
        if not PROVIDER_NAME.match(item.name) or item.socket is None or item.declared is None:
            raise ValueError("the gateway lists it without a usable name, socket or declaration")
        declared = parse_permissions(item.declared["permissions"])
        transport = SocketTransport(
            self._gateway.run_dir / item.socket, item.name, lambda: get_config().workspace_dir,
        )
        try:
            proxy = connect(transport, GatewayProviderContext(item.name, declared, self._grants))
        except ProviderCallFailed as exc:
            raise ValueError(f"it did not describe itself: {exc}") from exc
        manifest = proxy.manifest
        disagreements = [
            what for what, same in (
                ("name", manifest.name == item.name == item.declared["name"]),
                ("roles", set(manifest.roles) == set(item.declared["roles"])),
                ("provider API version", manifest.provider_api_version == PROVIDER_API_VERSION == item.declared["provider_api_version"]),
                ("permissions", manifest.permissions == declared),
            ) if not same
        ]
        if disagreements:
            self._rejected[item.name] = "what it says of itself differs from its image: " + ", ".join(disagreements)
            logger.warning("Refused gateway provider %s: %s", item.name, self._rejected[item.name])
            return
        self.register(proxy)

