"""
The host tells the gateway which providers are installed.

The database (``installed_providers`` and the ``provider_sources`` they came from) is the ONLY
source of truth for what is installed, and, with the root folders and the working folder (ADR-0008, ``root_folders`` and
the ``workspace_dir`` setting), for where the gateway may let a provider work: they are pushed with the set and judged by the
gateway, whose verdict on each root is recorded as it comes back. The gateway keeps no list: it runs what the host last
pushed over its control socket (``{"op": "install", ...}``, see ``gateway/musizeer_gateway/
desired.py``) and nothing before that. The host pushes

- when the API starts (``routes/deps.init_singletons``),
- after every install, update, removal and change of a source (``services/sources.py``), also
  from the maintenance command (``scripts/provider_sources.py``),
- after every change of a root folder or of a folder setting (``routes/roots.py``, ``routes/config.py``),
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
from services import roots as roots_service
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


def generation(entries: list[dict], roots: list[dict] | None = None, workspace: str | None = None) -> str:
    """An identifier of the set: a digest of everything the gateway acts on EXCEPT the credentials
    (so the identifier can be compared, logged and reported without holding a secret): the providers, the root folders
    and the working folder."""
    public = [{key: value for key, value in entry.items() if key != "credential"} for entry in entries]
    document = {"providers": public, "roots": sorted(roots or [], key=lambda root: root["id"]), "workspace": workspace}
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Desired:
    """Everything the host wants the gateway to hold: the providers, the root folders and the working folder."""

    entries: list[dict]
    roots: list[dict]
    workspace: str | None

    @property
    def generation(self) -> str:
        return generation(self.entries, self.roots, self.workspace)


def desired_state(session: Session) -> Desired:
    roots, workspace = roots_service.desired_folders(session)
    return Desired(desired_entries(session), roots, workspace)


@dataclass(frozen=True)
class PushResult:
    pushed: bool  # the gateway has the set (False: it could not be told; ``error`` says why)
    accepted: tuple[str, ...] = ()
    rejected: dict[str, str] = field(default_factory=dict)
    error: str = ""
    # What the gateway made of each root folder (``{id, state, reason, ...}``) and of the working folder.
    roots: tuple[dict, ...] = ()
    workspace: dict = field(default_factory=dict)


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
            return self._push(desired_state(session), session)
        with self._session_factory() as own:
            return self._push(desired_state(own), own)

    def _push(self, desired: Desired, session: Session) -> PushResult:
        try:
            reply = self._client.install(desired.entries, desired.generation, desired.roots, desired.workspace)
        except GatewayUnreachable as exc:
            level = logger.warning if self._client.deployed() else logger.info
            level("could not tell the gateway which providers are installed: %s", exc)
            return PushResult(False, error=str(exc))
        rejected = dict(reply.get("rejected") or {})
        for name, reason in rejected.items():
            logger.warning("the gateway refused the entry of %s: %s", name, reason)
        reports = tuple(reply.get("roots") or ())
        try:
            roots_service.record_verdicts(session, list(reports))
        except Exception as exc:  # noqa: BLE001 — the verdicts come back with the next listing
            logger.warning("could not record the gateway's verdict on the root folders: %s", exc)
            session.rollback()
        return PushResult(True, tuple(reply.get("accepted") or ()), rejected, roots=reports, workspace=dict(reply.get("workspace") or {}))

    def ensure(self, listing: GatewayListing) -> bool:
        """Push if the gateway holds another set than the database's (a restarted gateway holds
        none). True if it pushed. The gateway's verdicts on the root folders are recorded either way."""
        with self._session_factory() as session:
            desired = desired_state(session)
            if listing.roots:
                try:
                    roots_service.record_verdicts(session, list(listing.roots))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("could not record the gateway's verdict on the root folders: %s", exc)
                    session.rollback()
                else:
                    # a verdict can change which working folder is worth pushing (a refused root holds none)
                    desired = desired_state(session)
            if listing.desired == desired.generation:
                return False
            return self._push(desired, session).pushed


def _default_session() -> Session:
    from db.engine import engine

    return Session(engine)
