from __future__ import annotations
from dataclasses import dataclass
from typing import Annotated, Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from core.auth_manager import AuthManager
from core.provider_cooldown import ProviderCooldown
from services.config_manager import ConfigManager
from core.search_aggregator import SearchAggregator
from core.settings_resolver import provider_config_from_system_settings
from core import user_auth
from db.auth_store import CredentialStore
from db.engine import get_session
from db.settings_store import SettingsRepository
from db import user_service
from db.models import SYSTEM_USER_ID, User
from services.auth import auth_manager_for
from services.capabilities import Viewer, favorite_on_arrival
from services.credentials import shared_store, user_store
from services import provider_order
from services.provider_cooldown import RedisProviderCooldown
from services.gateway_sync import GatewaySync
from services.provider_events import GatewayWatcher, ProviderReloader
from services.provider_registry import ProviderRegistry
from services.secret_settings import seal_declared_secrets
from services.sources import SourceManager

# ------------------------------------------------------------------
# Singletons — inizializzati una volta all'avvio dell'app in main.py
# ------------------------------------------------------------------

_registry: ProviderRegistry | None = None
_config: ConfigManager | None = None


def init_singletons() -> None:
    """Chiamato in main.py all'evento startup."""
    global _registry, _config
    _config = ConfigManager()
    gateway_sync = GatewaySync()
    # The gateway runs what the database says is installed, and nothing until it is told.
    if gateway_sync.client.deployed():
        gateway_sync.push()
    _registry = ProviderRegistry()
    registry = _registry
    # The secret settings the providers declare and an older version stored in clear are sealed
    # now that the providers are known, and again whenever they change (docs/credentials.md).
    seal_declared_secrets(registry)
    # The API rebuilds its registry when the providers change (every process does), and is the
    # one that notices the gateway's set changed (services/provider_events.py). Both end with
    # the process.
    ProviderReloader(_registry, after_reload=lambda: seal_declared_secrets(registry)).start()
    GatewayWatcher(_registry, sync=gateway_sync).start()


def get_config() -> ConfigManager:
    if _config is None:
        raise RuntimeError("ConfigManager non inizializzato")
    return _config


def get_registry() -> ProviderRegistry:
    if _registry is None:
        raise RuntimeError("ProviderRegistry non inizializzato")
    return _registry


# ------------------------------------------------------------------
# Dipendenze per-request
# ------------------------------------------------------------------

SessionDep = Annotated[Session, Depends(get_session)]
ConfigDep = Annotated[ConfigManager, Depends(get_config)]
RegistryDep = Annotated[ProviderRegistry, Depends(get_registry)]


def get_stream_cooldown(config: ConfigDep) -> ProviderCooldown:
    """The rest of a storage that did not answer while someone was listening
    (``storage_stream_cooldown_seconds``; 0 = never): ``/stream`` plays the
    sync provider's copy meanwhile."""
    seconds = config.storage_stream_cooldown_seconds
    return RedisProviderCooldown(errors=1 if seconds > 0 else 0, seconds=seconds, scope="storage-stream")


StreamCooldownDep = Annotated[ProviderCooldown, Depends(get_stream_cooldown)]

_bearer = HTTPBearer(auto_error=False)


def get_shared_store(session: SessionDep) -> CredentialStore:
    """The instance's own accounts: for the routes an admin manages them through."""
    return shared_store(session)


# ------------------------------------------------------------------
# User auth dependencies
# ------------------------------------------------------------------

_SYSTEM_USER = User(
    id=SYSTEM_USER_ID,
    username="system",
    password_hash="",
    role="admin",
    is_active=True,
)


def get_current_user(
    session: SessionDep,
    config: ConfigDep,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)] = None,
) -> User:
    """Return the authenticated User, or a synthetic system user when
    ``auth_enabled`` is ``False`` in the config.  This keeps existing
    deployments working without any login flow.
    """
    if not config.auth_enabled:
        return _SYSTEM_USER

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
        )
    try:
        payload = user_auth.decode_access_token(credentials.credentials)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    user = user_service.get_user_by_id(session, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUserDep) -> User:
    """Dependency that ensures the current user has the admin role."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user


AdminUserDep = Annotated[User, Depends(require_admin)]


def get_optional_user(
    session: SessionDep,
    config: ConfigDep,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)] = None,
) -> Optional[User]:
    """Return the authenticated User if a valid token is present, else None.

    Never raises — for endpoints that allow anonymous access but still want
    to attribute the action to a logged-in caller when one is present. When auth
    is disabled there's no meaningful notion of "anonymous" (everyone is the
    same unauthenticated system), so this returns None rather than a synthetic
    user.
    """
    if not config.auth_enabled or credentials is None:
        return None
    try:
        payload = user_auth.decode_access_token(credentials.credentials)
    except Exception:
        return None
    user = user_service.get_user_by_id(session, payload["sub"])
    if user is None or not user.is_active:
        return None
    return user


OptionalUserDep = Annotated[Optional[User], Depends(get_optional_user)]


def get_credential_store(session: SessionDep, config: ConfigDep, user: OptionalUserDep) -> CredentialStore:
    """The accounts the VIEWER's calls use (``musizerr_sdk.auth.AccountModel``): their
    own when the provider has personal accounts and they connected one, else
    the shared one. Not signed in: the shared accounts only. With
    authentication off the viewer is the instance's synthetic user."""
    return user_store(session, SYSTEM_USER_ID if not config.auth_enabled else user.id if user else None)


def get_auth_manager(
    store: Annotated[CredentialStore, Depends(get_credential_store)],
) -> AuthManager:
    return auth_manager_for(store)


@dataclass(frozen=True)
class Downloader:
    """Chi avvia un download. ``requested_by`` è l'utente a cui attribuire la
    richiesta (``None`` con l'autenticazione disattivata: nessuna identità);
    ``user`` è sempre chi chiama (l'utente di sistema in modalità aperta), a
    cui va il preferito all'arrivo."""

    user: User

    @property
    def requested_by(self) -> Optional[str]:
        return None if self.user.id == SYSTEM_USER_ID else self.user.id

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"

    def check_force(self, force: bool) -> None:
        """Riscaricare una traccia già in libreria è una decisione da admin."""
        if force and not self.is_admin:
            raise HTTPException(status_code=403, detail="Only an admin can download a track already in the library")

    def favorite_for(self, requested: Optional[bool]) -> Optional[str]:
        """L'utente a cui la traccia va nei preferiti quando arriva, o None
        per un download semplice (solo admin: vedi
        ``services.capabilities.favorite_on_arrival``)."""
        return self.user.id if favorite_on_arrival(self.user, requested) else None


def get_downloader(user: CurrentUserDep) -> Downloader:
    """Chi avvia un download. Con l'autenticazione attiva serve un utente
    autenticato (401 altrimenti, come ogni rotta ``CurrentUserDep``)."""
    return Downloader(user)


DownloaderDep = Annotated[Downloader, Depends(get_downloader)]


def get_viewer(config: ConfigDep, user: OptionalUserDep) -> Viewer:
    """Chi guarda una lista di tracce (preferiti, ascolto completo): mai un
    rifiuto, un ospite ha solo lo stato condiviso."""
    return Viewer.of(config.auth_enabled, user)


ViewerDep = Annotated[Viewer, Depends(get_viewer)]


def get_stream_user(
    request: Request,
    session: SessionDep,
    config: ConfigDep,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)] = None,
) -> User:
    """Dependency for the full-audio routes (routes/stream.py).

    Accepts the streaming cookie (`POST /auth/account/stream-session`) or,
    for API clients, a bearer access token — never one in place of the other
    (distinct JWT `type`s, see core/user_auth.py). 401 otherwise.
    """
    if not config.auth_enabled:
        return _SYSTEM_USER

    raw_cookie = request.cookies.get(user_auth.STREAM_COOKIE_NAME)
    if raw_cookie:
        try:
            payload = user_auth.decode_stream_token(raw_cookie)
            user = user_service.get_user_by_id(session, payload["sub"])
        except (jwt.PyJWTError, KeyError):
            user = None
        if user is not None and user.is_active:
            return user

    if credentials is not None:
        try:
            payload = user_auth.decode_access_token(credentials.credentials)
            user = user_service.get_user_by_id(session, payload["sub"])
        except (jwt.PyJWTError, KeyError):
            user = None
        if user is not None and user.is_active:
            return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid stream authentication",
    )


StreamUserDep = Annotated[User, Depends(get_stream_user)]


def require_stream_admin(user: StreamUserDep) -> User:
    """The full-audio gate (``get_stream_user``: the streaming cookie or a
    bearer token) for a file only an admin may hear, e.g. one file of a
    duplicate group. 403 for any other role."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user


StreamAdminDep = Annotated[User, Depends(require_stream_admin)]


# ------------------------------------------------------------------
# Aggregated dependencies
# ------------------------------------------------------------------

def get_metadata_priority(
    registry: RegistryDep, config: ConfigDep, session: SessionDep, user: OptionalUserDep,
) -> list[str]:
    """The order in which the VIEWER asks the metadata providers: ``services/provider_order``,
    the one place that computes it (their own stored order else the system default, restricted
    to the registered providers, the rest appended in installation order)."""
    return provider_order.effective_metadata_priority(registry, config, session, user.id if user else None)


def get_system_metadata_priority(registry: RegistryDep, config: ConfigDep, session: SessionDep) -> list[str]:
    """The same order for no one in particular (the system default): for what is cached and shared
    across viewers."""
    return provider_order.effective_metadata_priority(registry, config, session)


MetadataPriorityDep = Annotated[list[str], Depends(get_metadata_priority)]
SystemMetadataPriorityDep = Annotated[list[str], Depends(get_system_metadata_priority)]


def get_search_aggregator(
    registry: RegistryDep,
    config: ConfigDep,
    session: SessionDep,
    user: CurrentUserDep,
    priority: MetadataPriorityDep,
) -> SearchAggregator:
    auth_manager = auth_manager_for(user_store(session, user.id))
    limits = {}
    system_settings = SettingsRepository(session).all_system()
    for p in registry.all_metadata():
        cfg = provider_config_from_system_settings(p.name, system_settings)
        if cfg.get("search_limit") is not None:
            limits[p.name] = int(cfg["search_limit"])
    return SearchAggregator(
        registry=registry,
        auth_manager=auth_manager,
        provider_priority=priority,
        provider_search_limits=limits,
        search_budget_seconds=config.search_provider_budget_seconds,
        default_search_limit=config.search_default_limit,
    )


def get_source_manager(session: SessionDep) -> SourceManager:
    return SourceManager(session)


CredentialStoreDep = Annotated[CredentialStore, Depends(get_credential_store)]
SharedStoreDep = Annotated[CredentialStore, Depends(get_shared_store)]
AuthManagerDep = Annotated[AuthManager, Depends(get_auth_manager)]
SearchAggregatorDep = Annotated[SearchAggregator, Depends(get_search_aggregator)]
SourceManagerDep = Annotated[SourceManager, Depends(get_source_manager)]
