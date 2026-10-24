"""
Downloaded files waiting for the storage (ADR-0003 workspaces, one step
further): when the upload to the storage provider fails transiently (a
timeout, a server that does not answer), the file the download provider wrote
is not thrown away. The host moves it to a per-track folder under
``<workspace_dir>/held-uploads/`` and the next attempt of that track's
download uploads it instead of downloading it again (``workers.tasks``).

The folder is host data, never seen by a provider: the storage provider gets
the file as a ``WorkspaceFile`` of it, like any workspace. ``remove_stale``
drops the files held longer than ``held_upload_max_age_hours`` (the download
is then done again from scratch); ``remove_stale_workspaces`` never touches
this folder.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from musizeer_sdk.interfaces import WorkspaceFile
from services.workspace import HELD_UPLOADS_DIRNAME, Workspace, WorkspaceError

logger = logging.getLogger(__name__)

_META = "held.json"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class HeldUpload:
    workspace: Workspace
    file_name: str
    provider: str | None
    confidence: str | None

    def file(self) -> WorkspaceFile:
        return self.workspace.file(self.file_name)


def _root(workspace_dir) -> Path:
    return Path(workspace_dir) / HELD_UPLOADS_DIRNAME


def _folder(workspace_dir, track_id: str) -> Path:
    name = _SAFE.sub("_", track_id)
    if name in ("", ".", ".."):
        raise WorkspaceError(f"invalid track id for a held upload: {track_id!r}")
    return _root(workspace_dir) / name


def hold(workspace_dir, track_id: str, downloaded: WorkspaceFile, provider: str | None,
         confidence: str | None) -> HeldUpload:
    """Moves the downloaded file out of its workspace (which is about to be
    removed) into the track's held folder. A file already held there (the
    retry of an upload) stays where it is."""
    folder = _folder(workspace_dir, track_id)
    source = Path(downloaded.path)
    if source.parent.resolve() != folder.resolve():
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        shutil.move(str(source), str(folder / source.name))
    meta = {"file": source.name, "provider": provider, "confidence": confidence,
            "held_at": datetime.now(timezone.utc).isoformat()}
    (folder / _META).write_text(json.dumps(meta))
    logger.info("Upload of %s held for a retry: %s", track_id, source.name)
    return HeldUpload(Workspace(folder), source.name, provider, confidence)


def find(workspace_dir, track_id: str) -> HeldUpload | None:
    """The file held for this track, if any (and still there)."""
    folder = _folder(workspace_dir, track_id)
    try:
        meta = json.loads((folder / _META).read_text())
        held = HeldUpload(Workspace(folder), meta["file"], meta.get("provider"), meta.get("confidence"))
        held.file()  # still there, inside its folder
        return held
    except (OSError, ValueError, KeyError, WorkspaceError):
        return None


def release(workspace_dir, track_id: str) -> None:
    """The file reached the storage (or is no longer wanted)."""
    shutil.rmtree(_folder(workspace_dir, track_id), ignore_errors=True)


def remove_stale(workspace_dir, max_age_seconds: float, now: float | None = None) -> int:
    """Removes the held files older than ``max_age_seconds``; returns how many."""
    root = _root(workspace_dir)
    if not root.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for entry in root.iterdir():
        try:
            if entry.is_dir() and now - entry.stat().st_mtime > max_age_seconds:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError as exc:
            logger.warning("Could not sweep held upload %s: %s", entry, exc)
    return removed
