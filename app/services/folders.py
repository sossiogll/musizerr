"""
Folder settings, resolved against the root folders (ADR-0008): the one place that turns a stored folder value
(``{"root_id", "path"}``) into the absolute folder it names, and that says whether the folders the product needs are set.

The rules of a value are ``core/folders.py`` (pure); the roots are the database (``db/root_store.py``); what the gateway
makes of them is recorded by ``services/roots.py``. This module is what the rest of the host asks:

- ``working_folder(config)`` is the working folder as a ``Path``, or it raises :class:`FoldersNotReady`
  (``working_folder_not_set``). ``ConfigManager.workspace_dir`` is exactly this, so every place that needs a workspace
  (the download worker, held uploads, streaming a stored file, the library scan, the full-sync probe) is blocked the same way
  while the folder is not set, without each one knowing;
- ``require_ready(...)`` is the check of the ONE download gate: the working folder and, when the active storage provider
  declares a library folder, that one too (``library_folder_not_set``);
- ``status(...)`` is what the viewer status reports (``GET /auth/account/status``) so the shell can warn;
- ``resolve_provider_settings`` is how a provider's folder settings become the plain absolute paths it receives in its
  context: the provider never sees a root.

A folder is "set" only when its value is valid, its root exists and the gateway has not refused the root: a root the gateway
refuses counts as not set, and the reason says why.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from sqlmodel import Session

from core import folders as rules
from core.settings_resolver import namespace_provider_key
from db import root_store

WORKING_FOLDER_KEY = "workspace_dir"

# The stable codes of a blocked download (``detail.error_code`` of the routes).
WORKING_FOLDER_NOT_SET = "working_folder_not_set"
LIBRARY_FOLDER_NOT_SET = "library_folder_not_set"

# Why a folder is not set (``FolderStatus.state``).
SET = "set"
NOT_SET = "not_set"
ROOT_MISSING = "root_missing"
ROOT_REFUSED = "root_refused"
INVALID = "invalid"


class FoldersNotReady(Exception):
    """A folder the product needs is not set: ``error_code`` is ``working_folder_not_set`` or ``library_folder_not_set``,
    ``detail`` why (a refused root's reason, a conversion that could not express the old value)."""

    def __init__(self, error_code: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.detail = detail


_MESSAGES = {
    WORKING_FOLDER_NOT_SET: "The working folder is not set: downloads are blocked until an administrator sets it (Administration > Root folders).",
    LIBRARY_FOLDER_NOT_SET: "The library folder is not set: nothing downloaded could be stored, so downloads are blocked until an administrator sets it.",
}


@contextmanager
def session_scope(session: Session | None = None, engine=None) -> Iterator[Session]:
    """``session`` if one is given, else one of its own on ``engine`` or the shared one (a caller with no session of its
    own: a provider's context, a worker reading the working folder)."""
    if session is not None:
        yield session
        return
    if engine is None:
        from db.engine import engine as shared

        engine = shared
    with Session(engine) as owned:
        yield owned


def real_path(path: str) -> str | None:
    """The real path of ``path`` where this process runs, or ``None`` when it is not there (a folder only the gateway
    sees)."""
    try:
        return os.path.realpath(path) if os.path.lexists(path) else None
    except OSError:
        return None


@dataclass(frozen=True)
class RootView:
    """A root folder as the rest of the host reads it: plain data, free of the session it was read in."""

    id: str
    label: str
    path: str
    state: str
    reason: str


def roots_by_id(session: Session | None = None, engine=None) -> dict[str, RootView]:
    with session_scope(session, engine) as scope:
        return {
            root.id: RootView(root.id, root.label, root.path, root.state, root.reason)
            for root in root_store.list_roots(scope)
        }


# ------------------------------------------------------------------
# One value
# ------------------------------------------------------------------


@dataclass(frozen=True)
class FolderStatus:
    """Whether one folder setting is set, and if not why (``state``, ``detail``)."""

    state: str
    detail: str = ""
    root_id: str | None = None
    path: str | None = None
    resolved: str | None = None

    @property
    def is_set(self) -> bool:
        return self.state == SET


def judge(raw: Any, roots: Mapping[str, RootView], real=real_path) -> FolderStatus:
    """The status of the stored value ``raw`` against ``roots``."""
    if rules.is_unset(raw):
        return FolderStatus(NOT_SET)
    try:
        value = rules.parse_value(raw)
    except rules.FolderError as refused:
        return FolderStatus(NOT_SET, f"the stored value is not a folder: {refused.message}")
    root = roots.get(value.root_id)
    if root is None:
        return FolderStatus(ROOT_MISSING, "its root folder does not exist (any more)", value.root_id, value.path)
    if root.state == "refused":
        return FolderStatus(ROOT_REFUSED, root.reason or "the gateway refuses its root folder", value.root_id, value.path)
    try:
        resolved = rules.resolve(root.path, value.path, real)
    except rules.FolderError as refused:
        return FolderStatus(INVALID, refused.message, value.root_id, value.path)
    return FolderStatus(SET, "", value.root_id, value.path, resolved)


def resolve_or_none(raw: Any, roots: Mapping[str, RootView] | None = None, session: Session | None = None) -> str | None:
    """The absolute folder ``raw`` names, or ``None`` when it is not set (never raises: for a context that must carry what
    there is)."""
    if rules.is_unset(raw):
        return None
    if roots is None:
        roots = roots_by_id(session)
    return judge(raw, roots).resolved


# ------------------------------------------------------------------
# The working folder
# ------------------------------------------------------------------


def _conversion_note(session: Session | None, key: str, engine=None) -> str:
    """Why the conversion of the old value of ``key`` left it unset, if it did (``services/roots.py``)."""
    with session_scope(session, engine) as scope:
        record = root_store.state_get(scope, "folders_conversion", {}) or {}
    return str((record.get("unset") or {}).get(key, ""))


def working_folder(config, session: Session | None = None) -> Path:
    """The working folder, or :class:`FoldersNotReady` (``working_folder_not_set``)."""
    raw = config.get(WORKING_FOLDER_KEY)
    engine = getattr(config, "_engine", None)  # a ConfigManager made for one database (tests); the shared one otherwise
    status = judge(raw, roots_by_id(session, engine))
    if status.is_set:
        assert status.resolved is not None
        return Path(status.resolved)
    detail = status.detail or _conversion_note(session, WORKING_FOLDER_KEY, engine)
    raise FoldersNotReady(WORKING_FOLDER_NOT_SET, _MESSAGES[WORKING_FOLDER_NOT_SET], detail)


# ------------------------------------------------------------------
# The library folder (the active storage provider's)
# ------------------------------------------------------------------


def folder_keys(schema: Iterable[Any]) -> list[str]:
    """The short keys of the ``folder`` settings in a provider's ``config_schema()``."""
    return [entry.key for entry in schema if getattr(entry, "type", None) == "folder"]


def library_setting_key(provider) -> str | None:
    """The short key of the folder setting that is ``provider``'s library: the one it declares with
    ``library_folder_setting()`` or, when it declares none, its only ``folder`` setting; ``None`` for a provider whose
    library is not a folder of this machine."""
    declared = provider.library_folder_setting()
    if declared:
        return declared
    keys = folder_keys(provider.config_schema())
    return keys[0] if len(keys) == 1 else None


def active_storage(config, registry):
    name = config.storage_provider
    return registry.get_storage_provider(name) if name else None


@dataclass(frozen=True)
class LibraryStatus:
    """The library folder: ``applicable`` is false when no storage provider is active or its library is not a folder."""

    applicable: bool
    status: FolderStatus = FolderStatus(SET)
    provider: str | None = None
    setting: str | None = None  # the short key


def library_status(session: Session, config, registry) -> LibraryStatus:
    provider = active_storage(config, registry)
    if provider is None:
        return LibraryStatus(False)
    key = library_setting_key(provider)
    if key is None:
        return LibraryStatus(False, provider=provider.name)
    from db.settings_store import SettingsRepository

    raw = SettingsRepository(session).get_system(namespace_provider_key(provider.name, key))
    status = judge(raw, roots_by_id(session))
    if not status.is_set and not status.detail:
        status = FolderStatus(status.state, _conversion_note(session, namespace_provider_key(provider.name, key)), status.root_id, status.path)
    return LibraryStatus(True, status, provider.name, key)


# ------------------------------------------------------------------
# Readiness
# ------------------------------------------------------------------


def require_ready(session: Session, config, registry) -> None:
    """The check of the one download gate: raises :class:`FoldersNotReady` when downloads cannot work, the working folder
    first. Nothing is read from disk, and nothing is queued or recorded by a caller that stops here."""
    config.workspace_dir  # noqa: B018 — raises FoldersNotReady while the working folder is not set
    library = library_status(session, config, registry)
    if library.applicable and not library.status.is_set:
        raise FoldersNotReady(LIBRARY_FOLDER_NOT_SET, _MESSAGES[LIBRARY_FOLDER_NOT_SET], library.status.detail)


def blocked(session: Session, config, registry) -> FoldersNotReady | None:
    """What blocks downloads, or ``None``: :func:`require_ready` as a value."""
    try:
        require_ready(session, config, registry)
    except FoldersNotReady as blocking:
        return blocking
    return None


def status(session: Session, config, registry, *, detailed: bool) -> dict[str, Any]:
    """What the viewer status carries (``GET /auth/account/status``, ``folders``): whether downloads can work and which
    folder is missing. ``detailed`` (administrators) adds the setting that holds each, why it is not set, and the root."""
    try:
        config.workspace_dir  # noqa: B018
        working = FolderStatus(SET)
    except FoldersNotReady as missing:
        judged = judge(config.get(WORKING_FOLDER_KEY), roots_by_id(session))
        working = FolderStatus(NOT_SET if judged.is_set else judged.state, missing.detail, judged.root_id, judged.path)
    library = library_status(session, config, registry)

    def view(is_set: bool, state: str, detail: str, setting: str | None, applicable: bool = True) -> dict[str, Any]:
        entry: dict[str, Any] = {"set": is_set, "state": state, "applicable": applicable}
        if detailed:
            entry["setting"] = setting
            entry["detail"] = detail
        return entry

    working_view = view(working.is_set, working.state, working.detail, WORKING_FOLDER_KEY)
    library_view = view(
        library.status.is_set if library.applicable else True, library.status.state if library.applicable else SET,
        library.status.detail, namespace_provider_key(library.provider, library.setting) if library.provider and library.setting else None,
        library.applicable,
    )
    missing_folders = [name for name, entry in (("working_folder", working_view), ("library_folder", library_view)) if not entry["set"]]
    return {
        "ready": not missing_folders,
        "missing": missing_folders,
        "working_folder": working_view,
        "library_folder": library_view,
    }


# ------------------------------------------------------------------
# A provider's folders, for its context
# ------------------------------------------------------------------


def resolve_provider_settings(
    settings: Mapping[str, Any], keys: Iterable[str], roots: Mapping[str, RootView] | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """``settings`` (a provider's, by short key) with each folder setting in ``keys`` replaced by the plain ABSOLUTE path it
    names. A folder that is not set, or cannot be resolved, is left OUT: the provider receives no value for it and answers
    "not configured". The provider never learns that roots exist."""
    folder_keys_ = [key for key in keys if key in settings]
    result = dict(settings)
    if not folder_keys_:
        return result
    if roots is None:
        roots = roots_by_id(session)
    for key in folder_keys_:
        resolved = judge(result.pop(key), roots).resolved
        if resolved is not None:
            result[key] = resolved
    return result
