"""
The host tells the gateway which providers are installed.

The database (``installed_providers`` and the ``provider_sources`` they came from) is the ONLY
source of truth for what is installed. The gateway keeps no list: it runs what the host last
pushed over its control socket (``{"op": "install", ...}``, see ``gateway/musizeer_gateway/
desired.py``) and nothing before that. The host pushes

- when the API starts (``routes/deps.init_singletons``),
- after every install, update, removal and change of a source (``services/sources.py``), also
  from the maintenance command (``scripts/provider_sources.py``),
- whenever the gateway answers that it has another set than the database's — which is also
  what happens when it restarted (it knows nothing then): ``provider_events.GatewayWatcher``
  compares the ``desired`` identifier of ``list`` with :func:`generation` every few seconds and
  pushes the difference.

Each entry carries what the admin APPROVED (roles and permissions): the gateway refuses an
image whose labels declare anything else. An entry carries its source's credential, if the
source has one, only to let the gateway pull that image; the identifier of a set
(:func:`generation`, a digest of everything but the credentials) never covers it, and neither
does anything this module logs or returns.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Callable

from sqlmodel import Session, select

from db.models import InstalledProvider, ProviderSource
from services.gateway_client import GatewayClient, GatewayListing, GatewayUnreachable

logger = logging.getLogger(__name__)


def desired_entries(session: Session) -> list[dict]:
    """The installed providers as the gateway wants them (the credential, if any, included),
    in a stable order."""
    sources = {source.id: source for source in session.exec(select(ProviderSource)).all()}
    entries = []
    for row in session.exec(select(InstalledProvider).order_by(InstalledProvider.name)).all():
        source = sources.get(row.source_id)
        entry: dict = {
            "name": row.name,
            "image": row.image,
            "digest": row.digest,
            "roles": json.loads(row.roles),
            "permissions": json.loads(row.permissions),
            "insecure": bool(source and source.allow_insecure_registry),
        }
        if source is not None and source.credential:
            entry["credential"] = source.credential
        entries.append(entry)
    return entries


def generation(entries: list[dict]) -> str:
    """An identifier of the set: a digest of everything the gateway acts on EXCEPT the credentials
    (so the identifier can be compared, logged and reported without holding a secret)."""
    public = [{key: value for key, value in entry.items() if key != "credential"} for entry in entries]
    return hashlib.sha256(json.dumps(public, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class PushResult:
    pushed: bool  # the gateway has the set (False: it could not be told; ``error`` says why)
    accepted: tuple[str, ...] = ()
    rejected: dict[str, str] = field(default_factory=dict)
    error: str = ""


class GatewaySync:
    def __init__(self, client: GatewayClient | None = None, session_factory: Callable[[], Session] | None = None) -> None:
        self._client = client or GatewayClient()
        self._session_factory = session_factory or _default_session

    @property
    def client(self) -> GatewayClient:
        return self._client

    def push(self, session: Session | None = None) -> PushResult:
        """Tell the gateway the database's set. Never raises: a gateway that cannot be reached is
        told again by the watcher when it comes back."""
        if session is not None:
            return self._push(desired_entries(session))
        with self._session_factory() as own:
            return self._push(desired_entries(own))

    def _push(self, entries: list[dict]) -> PushResult:
        try:
            reply = self._client.install(entries, generation(entries))
        except GatewayUnreachable as exc:
            level = logger.warning if self._client.deployed() else logger.info
            level("could not tell the gateway which providers are installed: %s", exc)
            return PushResult(False, error=str(exc))
        rejected = dict(reply.get("rejected") or {})
        for name, reason in rejected.items():
            logger.warning("the gateway refused the entry of %s: %s", name, reason)
        return PushResult(True, tuple(reply.get("accepted") or ()), rejected)

    def ensure(self, listing: GatewayListing) -> bool:
        """Push if the gateway holds another set than the database's (a restarted gateway holds
        none). True if it pushed."""
        with self._session_factory() as session:
            entries = desired_entries(session)
        if listing.desired == generation(entries):
            return False
        return self._push(entries).pushed


def _default_session() -> Session:
    from db.engine import engine

    return Session(engine)
