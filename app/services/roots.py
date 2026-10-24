"""
Root folders, the use cases (ADR-0008): declaring, renaming, moving and removing a root; the gateway's verdicts on them; the
checks the interface shows; and the one-shot conversion of the folder values an older version stored.

The rules of a path and of a folder value are ``core/folders.py``; the table is ``db/root_store.py``; turning a stored value
into a folder is ``services/folders.py``. Only the gateway sees every mount, so it alone judges a root
(``gateway/musizeer_gateway/roots.py``): the host saves what the administrator declares, pushes it
(``services/gateway_sync.py``) and records the verdict that comes back (``state``, ``reason``). A root the gateway refuses is
saved and not used; a folder setting that points at it counts as not set, with the reason.

A root in use (a folder setting points at it) cannot be removed (409, with the settings listed), and moving it (a mount that
moved) needs a confirmation that lists them.

**The conversion** (``convert_legacy``) runs from ``GatewayWatcher.check`` when the gateway answers. It turns the working
folder an older version stored (migration ``0046`` set it aside in ``host_state``) and the strings stored under the settings
that providers now declare as ``folder`` into a root plus a path, creating the root when none contains the folder and the
gateway says the path is a mounted, writable folder. Anything it cannot express is left UNSET, with the reason (it is the
blocked state of ADR-0008, with its warning); it never invents a default. It is idempotent and records what it did.
"""
from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlmodel import Session

from core import folders as rules
from core.settings_catalog import SettingDef
from core.settings_resolver import merged_schema
from db import root_store
from db.models import RootFolder
from db.settings_store import SettingsRepository
from services import folders
from services.gateway_client import GatewayClient, GatewayRefused, GatewayUnreachable

logger = logging.getLogger(__name__)

LEGACY_KEY = "legacy_workspace_dir"
CONVERSION_KEY = "folders_conversion"
# What the working folder is called when the conversion moves an old default (the old hidden name) forward.
NEW_LEAF = ".musizeer-work"


class RootError(Exception):
    """A refusal with a stable ``code`` (``error_code`` of the routes), the HTTP ``status`` and, where it helps, ``details``."""

    def __init__(self, code: str, message: str, status: int = 400, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


# ------------------------------------------------------------------
# What points at a root
# ------------------------------------------------------------------


def folder_settings(registry) -> list[SettingDef]:
    """Every setting of type ``folder``: the working folder and each registered provider's."""
    return [setting for setting in merged_schema(registry) if setting.type == "folder"]


def usages(session: Session, registry, root_id: str | None = None) -> list[dict[str, Any]]:
    """The folder settings and what each currently holds, or only those that point at ``root_id``."""
    repository = SettingsRepository(session)
    found = []
    for setting in folder_settings(registry):
        value = rules.value_or_none(repository.get_system(setting.key))
        if root_id is not None and (value is None or value.root_id != root_id):
            continue
        found.append({
            "key": setting.key, "label": setting.label, "group": setting.group,
            "root_id": value.root_id if value else None, "path": value.path if value else None,
        })
    return found


def view(root: RootFolder, used_by: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from core.utc import utc_iso

    return {
        "id": root.id, "label": root.label, "path": root.path, "state": root.state, "reason": root.reason,
        "checked_at": utc_iso(root.checked_at) if root.checked_at else None,
        "created_at": utc_iso(root.created_at),
        "used_by": [{key: item[key] for key in ("key", "label", "group")} for item in used_by or []],
    }


def folders_view(session: Session, registry) -> list[dict[str, Any]]:
    """The table of folders: every folder setting, its root and path, the folder it resolves to and its status."""
    roots = folders.roots_by_id(session)
    repository = SettingsRepository(session)
    result = []
    for setting in folder_settings(registry):
        raw = repository.get_system(setting.key)
        status = folders.judge(raw, roots)
        result.append({
            "key": setting.key, "label": setting.label, "group": setting.group,
            "root_id": status.root_id, "path": status.path, "resolved": status.resolved,
            "state": status.state, "detail": status.detail, "suggested_path": setting.suggested_path,
        })
    return result


# ------------------------------------------------------------------
# Declaring, moving, removing
# ------------------------------------------------------------------


def list_roots(session: Session, registry) -> list[dict[str, Any]]:
    all_usages = usages(session, registry)
    return [
        view(root, [item for item in all_usages if item["root_id"] == root.id])
        for root in root_store.list_roots(session)
    ]


def get_root(session: Session, root_id: str) -> RootFolder:
    root = root_store.get(session, root_id)
    if root is None:
        raise RootError("root_not_found", "That root folder does not exist", 404)
    return root


def create_root(session: Session, path: object, label: object = None) -> RootFolder:
    try:
        clean_path = rules.normalize_root_path(path)
        clean_label = rules.clean_label(label, rules.label_for(clean_path))
    except rules.FolderError as refused:
        raise RootError(refused.code, refused.message, 422) from refused
    try:
        return root_store.create(session, clean_label, clean_path)
    except root_store.RootExists:
        raise RootError("root_path_in_use", "Another root folder has this path", 409) from None


def update_root(session: Session, registry, root_id: str, *, path: object = None, label: object = None, confirm: bool = False) -> tuple[RootFolder, bool]:
    """Rename and/or move a root. Returns the root and whether its path changed (the caller then pushes it to the gateway).
    Moving a root in use needs ``confirm``; without it the refusal lists what depends on it."""
    root = get_root(session, root_id)
    try:
        new_path = rules.normalize_root_path(path) if path is not None else None
        new_label = rules.clean_label(label) if label is not None else None
    except rules.FolderError as refused:
        raise RootError(refused.code, refused.message, 422) from refused
    moved = new_path is not None and new_path != root.path
    if moved:
        used_by = usages(session, registry, root.id)
        if used_by and not confirm:
            raise RootError(
                "confirmation_required",
                "Moving a root folder in use changes where these folders are: confirm to go on",
                409, {"used_by": [{k: item[k] for k in ("key", "label", "group")} for item in used_by]},
            )
    try:
        updated = root_store.update(session, root, label=new_label, path=new_path if moved else None)
    except root_store.RootExists:
        raise RootError("root_path_in_use", "Another root folder has this path", 409) from None
    return updated, moved


def delete_root(session: Session, registry, root_id: str) -> None:
    root = get_root(session, root_id)
    used_by = usages(session, registry, root.id)
    if used_by:
        raise RootError(
            "root_in_use", "This root folder is in use: change the folders that point at it first", 409,
            {"used_by": [{k: item[k] for k in ("key", "label", "group")} for item in used_by]},
        )
    root_store.delete(session, root.id)


# ------------------------------------------------------------------
# The gateway's verdicts
# ------------------------------------------------------------------


def desired_folders(session: Session) -> tuple[list[dict[str, str]], str | None]:
    """What the host tells the gateway: ``[{id, path}]`` for every root, and the resolved working folder (None while it is
    not set, or its root is refused)."""
    roots = folders.roots_by_id(session)
    declared = [{"id": root.id, "path": root.path} for root in roots.values()]
    raw = SettingsRepository(session).get_system(folders.WORKING_FOLDER_KEY)
    return declared, folders.judge(raw, roots).resolved


def record_verdicts(session: Session, reports: list[dict]) -> int:
    """Store the gateway's verdict on each root (from the reply of an install, or a listing). Returns how many changed."""
    verdicts = {
        str(report["id"]): (str(report.get("state", "unchecked")), str(report.get("reason", "")))
        for report in reports if isinstance(report, dict) and "id" in report
    }
    return root_store.record_verdicts(session, verdicts) if verdicts else 0


def check(session: Session, client: GatewayClient, root_id: str) -> dict[str, Any]:
    """The explicit Check of a root: what the gateway sees at its path (with a write test) and whether the application
    sees it too. Nothing is stored here: the verdict is recorded by the push."""
    import os

    root = get_root(session, root_id)
    report: dict[str, Any] = {"root": view(root), "gateway": None, "gateway_error": None}
    try:
        report["gateway"] = client.inspect(root.path, write_test=True)
    except GatewayRefused as refused:
        report["gateway_error"] = refused.error
    except GatewayUnreachable as unreachable:
        report["gateway_error"] = str(unreachable)
    report["application"] = {
        "exists": os.path.isdir(root.path),
        "writable": os.path.isdir(root.path) and os.access(root.path, os.W_OK | os.X_OK),
    }
    gateway = report["gateway"]
    report["ok"] = bool(gateway and gateway["mounted"] and gateway["writable"] and root.state != "refused")
    return report


# ------------------------------------------------------------------
# The conversion of today's values
# ------------------------------------------------------------------


@dataclass
class ConversionReport:
    converted: dict[str, dict[str, str]] = field(default_factory=dict)  # setting key -> {root_id, path}
    unset: dict[str, str] = field(default_factory=dict)  # setting key -> why it was left unset
    created_roots: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.converted or self.unset)


class _Converter:
    def __init__(self, session: Session, client: GatewayClient) -> None:
        self.session = session
        self.client = client
        self.repository = SettingsRepository(session)
        self.report = ConversionReport()

    def _express(self, folder: str) -> tuple[str, str]:
        """``(root id, relative path)`` for the absolute ``folder``: under a root that already contains it, else a
        root made at the folder itself. The gateway must say the folder is a mounted, writable directory before a root is made."""
        known = rules.containing(root_store.paths_of(root_store.list_roots(self.session)), folder)
        if known is not None:
            return known
        return self._new_root(folder), ""

    def _new_root(self, path: str) -> str:
        try:
            seen = self.client.inspect(path)
        except GatewayRefused as refused:
            raise rules.FolderError(rules.ROOT_REFUSED, refused.error) from None
        if not (seen.get("directory") and seen.get("mounted") and seen.get("writable")):
            raise rules.FolderError(rules.ROOT_REFUSED, f"the gateway does not accept '{path}' as a root folder: {seen.get('reason') or 'not usable'}")
        existing = root_store.find_by_path(self.session, path)
        if existing is not None:
            return existing.id
        root = root_store.create(self.session, rules.label_for(path), path)
        self.report.created_roots.append(root.id)
        return root.id

    def working_folder(self, legacy: dict[str, Any]) -> None:
        key = folders.WORKING_FOLDER_KEY
        if not rules.is_unset(self.repository.get_system(key)):
            return  # the administrator set it first: nothing to convert
        stored = legacy.get("value") if isinstance(legacy.get("value"), str) and legacy.get("value") else None
        old = stored or legacy.get("default")
        if not isinstance(old, str):
            return
        try:
            parent, name = rules.legacy_working_folder(old, legacy.get("old_leaf") or "", NEW_LEAF)
            root_id, base = self._express(parent)
            relative = posixpath.join(base, name) if base else name
            value = rules.FolderValue(root_id, relative)
            rules.resolve(root_store.get(self.session, root_id).path, relative)
        except rules.FolderError as refused:
            self.report.unset[key] = f"the working folder '{old}' could not be kept: {refused.message}"
            return
        self.repository.set_system(key, value.stored())
        self.report.converted[key] = value.stored()

    def provider_folder(self, setting: SettingDef) -> None:
        raw = self.repository.get_system(setting.key)
        if not isinstance(raw, str):
            return
        text = raw.strip()
        if not text:
            self.repository.set_system(setting.key, None)
            return
        try:
            clean = rules.normalize_root_path(text)
            root_id, relative = self._express(clean)
            value = rules.FolderValue(root_id, relative)
        except rules.FolderError as refused:
            self.repository.set_system(setting.key, None)
            self.report.unset[setting.key] = f"the folder '{text}' could not be kept: {refused.message}"
            return
        self.repository.set_system(setting.key, value.stored())
        self.report.converted[setting.key] = value.stored()


def _note_unset_library(session: Session, registry, report: ConversionReport) -> None:
    """After the conversion of an install that had folders, the library folder of the active storage provider may still be
    unset (the host does not know a provider's old default, so a library that was never typed is not converted): the reason
    kept for the blocked state says where to set it."""
    from core.settings_resolver import namespace_provider_key

    name = SettingsRepository(session).get_system("storage_provider")
    provider = registry.get_storage_provider(name) if name else None
    key = folders.library_setting_key(provider) if provider is not None else None
    if key is None:
        return
    full = namespace_provider_key(provider.name, key)
    if rules.value_or_none(SettingsRepository(session).get_system(full)) is not None or full in report.converted:
        return
    hint = folders.library_hint(provider)
    previous = report.unset.get(full)
    report.unset[full] = f"{previous}; {hint}" if previous else hint


def pending(session: Session, registry) -> bool:
    """Is there a legacy value to convert? (cheap: it is asked at every check of the gateway)"""
    if root_store.state_get(session, LEGACY_KEY) is not None:
        return True
    repository = SettingsRepository(session)
    return any(isinstance(repository.get_system(setting.key), str) for setting in folder_settings(registry))


def convert_legacy(session: Session, client: GatewayClient, registry, on_written: Callable[[dict[str, Any]], None] | None = None) -> ConversionReport | None:
    """Run the conversion once (idempotent: what is converted is no longer a legacy value). Returns what it did, or None when
    there was nothing to do. ``on_written`` receives the settings written, so the caller can refresh its cache."""
    if not pending(session, registry):
        return None
    converter = _Converter(session, client)
    legacy = root_store.state_get(session, LEGACY_KEY)
    try:
        if isinstance(legacy, dict):
            converter.working_folder(legacy)
        for setting in folder_settings(registry):
            converter.provider_folder(setting)
    except GatewayUnreachable:
        session.rollback()
        return None  # the gateway went away midway: the next check starts again (nothing converted is lost)
    report = converter.report
    if isinstance(legacy, dict):
        root_store.state_delete(session, LEGACY_KEY)
        _note_unset_library(session, registry, report)
    if report.changed or report.created_roots:
        previous = root_store.state_get(session, CONVERSION_KEY, {}) or {}
        unset = {**(previous.get("unset") or {}), **report.unset}
        for key in report.converted:
            unset.pop(key, None)
        root_store.state_set(session, CONVERSION_KEY, {
            "done_at": datetime.now(timezone.utc).isoformat(), "unset": unset,
            "converted": {**(previous.get("converted") or {}), **report.converted},
        })
        for key, why in report.unset.items():
            logger.warning("Folder setting %s was left unset by the conversion: %s", key, why)
        for key in report.converted:
            logger.info("Folder setting %s was converted into a root folder and a path", key)
        if on_written is not None:
            on_written(report.converted)
    session.commit()
    return report
