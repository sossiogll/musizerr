"""
Cartelle di lavoro dell'host (ADR-0003): ogni operazione che scambia file con
un provider riceve una cartella vuota sotto ``workspace_dir``, in cui il
provider scrive o legge e da cui restituisce solo nomi relativi. L'host la
rimuove a fine operazione; ``remove_stale_workspaces`` rimuove quelle rimaste da un
processo interrotto.

**Una cartella è di UN provider**: sta in ``<workspace_dir>/<provider>/<casuale>``. Un
provider in una sandbox vede solo ``<workspace_dir>/<provider>`` (il gateway lo monta
a ``/workspace``), quindi nient'altro dell'host e nessun file di un altro provider.
Un file passa da un provider a un altro (scaricato da uno, salvato da un altro) come
un ``WorkspaceFile`` della cartella del primo: il trasporto verso il secondo lo porta
nella cartella del secondo (``services/provider_socket.py``).
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from musizerr_sdk.interfaces import StorageStreamResult, WorkspaceFile

logger = logging.getLogger(__name__)


# The folder of the downloaded files waiting for the storage
# (``services/held_uploads.py``): host data with its own retention, never a
# stale workspace.
HELD_UPLOADS_DIRNAME = "held-uploads"

# A provider's folder is named after it, and the name comes from the provider's own
# manifest: only a plain name is ever a directory.
PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class WorkspaceError(ValueError):
    """Un provider ha restituito un nome che esce dalla propria cartella."""


def provider_directory(root: Path, provider: str) -> Path:
    """``<root>/<provider>``, the subtree a provider's sandbox sees. The folder is
    traversable by every user of the volume but listable only by its owner: the host and
    the gateway may run as different users, and a call's folder is reachable only by
    its random name."""
    if not PROVIDER_NAME.match(provider) or provider == HELD_UPLOADS_DIRNAME:
        raise WorkspaceError(f"'{provider}' cannot name a workspace directory")
    directory = root / provider
    if not directory.is_dir():
        root.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir(mode=0o711)
            directory.chmod(0o711)
        except FileExistsError:
            pass
    return directory


class Workspace:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __str__(self) -> str:
        return str(self.path)

    def file(self, name: str) -> WorkspaceFile:
        """Il file ``name`` restituito da un provider, verificato: deve stare
        dentro questa cartella ed esistere."""
        target = (self.path / name).resolve()
        if not target.is_relative_to(self.path.resolve()) or target == self.path.resolve():
            raise WorkspaceError(f"'{name}' is outside the provider workspace")
        if not target.is_file():
            raise WorkspaceError(f"'{name}' does not exist in the provider workspace")
        return WorkspaceFile(workspace=str(self.path), name=str(target.relative_to(self.path.resolve())))

    def local_file(self, streamed: StorageStreamResult) -> str | None:
        """Path locale del file reso da ``StorageProvider.stream()`` in questa
        cartella: il file stesso, o i blocchi di byte scritti qui. None per un
        URL, che l'host non scarica."""
        if streamed.file:
            return self.file(streamed.file).path
        if streamed.chunks is not None:
            target = self.path / ("streamed" + Path(streamed.filename).suffix)
            with open(target, "wb") as f:
                for chunk in streamed.chunks:
                    f.write(chunk)
            return str(target)
        return None

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def new_workspace(root: Path, provider: str) -> Workspace:
    """Crea una cartella vuota per ``provider`` sotto ``root``; chi la crea la
    rimuove con ``cleanup()`` (o ``open_workspace`` lo fa da solo). Scrivibile da
    chiunque la conosca: il provider (la sua sandbox, un altro utente se il gateway
    gira come tale) deve potervi scrivere, e l'accesso lo regola la cartella del provider."""
    path = provider_directory(root, provider) / uuid.uuid4().hex
    path.mkdir()
    path.chmod(0o777)
    return Workspace(path)


@contextmanager
def open_workspace(root: Path, provider: str) -> Iterator[Workspace]:
    workspace = new_workspace(root, provider)
    try:
        yield workspace
    finally:
        workspace.cleanup()


class Workspaces:
    """The workspaces of one operation that may try several providers (a download tries
    them in turn): one per provider, made when it is first asked for, all removed together."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._by_provider: dict[str, Workspace] = {}
        self._lock = threading.Lock()

    def for_provider(self, provider: str) -> Workspace:
        with self._lock:
            if provider not in self._by_provider:
                self._by_provider[provider] = new_workspace(self._root, provider)
            return self._by_provider[provider]

    def path_for(self, provider: str) -> str:
        return str(self.for_provider(provider).path)

    def cleanup(self) -> None:
        with self._lock:
            for workspace in self._by_provider.values():
                workspace.cleanup()
            self._by_provider.clear()


@contextmanager
def open_workspaces(root: Path) -> Iterator[Workspaces]:
    workspaces = Workspaces(root)
    try:
        yield workspaces
    finally:
        workspaces.cleanup()


def remove_stale_workspaces(root: Path, max_age_seconds: float, now: float | None = None) -> int:
    """Rimuove le cartelle di lavoro (``<root>/<provider>/<casuale>``) più vecchie di
    ``max_age_seconds`` (mai quella degli upload in attesa, mai la cartella di un
    provider: il gateway la monta); ritorna quante ne ha rimosse."""
    if not root.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for provider_dir in root.iterdir():
        if provider_dir.name == HELD_UPLOADS_DIRNAME or not provider_dir.is_dir():
            continue
        try:
            entries = list(provider_dir.iterdir())
        except OSError as exc:
            logger.warning("Could not sweep workspaces of %s: %s", provider_dir, exc)
            continue
        for entry in entries:
            try:
                if entry.is_dir() and now - entry.stat().st_mtime > max_age_seconds:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError as exc:
                logger.warning("Could not sweep workspace %s: %s", entry, exc)
    return removed
