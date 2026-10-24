"""
Data access of the root folders (``RootFolder``) and of the host's own remembered facts (``HostState``).

Persistence only: what a path may be and what a folder value means are ``core/folders.py``; the gateway's verdicts and the
use cases are ``services/roots.py``. The path of a root is unique in the table, so two writers that create the same root
race on the constraint, not on a look-then-insert (``db/upsert.py``).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Mapping

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from db.models import HostState, RootFolder, _now
from db.upsert import insert_ignore, upsert


class RootExists(Exception):
    """Another root folder has this path."""


def list_roots(session: Session) -> list[RootFolder]:
    return list(session.exec(select(RootFolder).order_by(RootFolder.created_at, RootFolder.id)).all())


def get(session: Session, root_id: str) -> RootFolder | None:
    return session.get(RootFolder, root_id)


def find_by_path(session: Session, path: str) -> RootFolder | None:
    return session.exec(select(RootFolder).where(RootFolder.path == path)).first()


def create(session: Session, label: str, path: str) -> RootFolder:
    """The new root; :class:`RootExists` if another has this path. Committed."""
    root = RootFolder(label=label, path=path)
    if insert_ignore(session, root) == 0:
        session.rollback()
        raise RootExists(path)
    session.commit()
    created = find_by_path(session, path)
    assert created is not None
    return created


def update(session: Session, root: RootFolder, *, label: str | None = None, path: str | None = None) -> RootFolder:
    """Rename a root and/or move it to another path (its verdict is forgotten: the gateway judges the new path).
    :class:`RootExists` if another root has the new path. Committed."""
    if label is not None:
        root.label = label
    if path is not None and path != root.path:
        root.path = path
        root.state, root.reason, root.checked_at = "unchecked", "", None
    root.updated_at = _now()
    session.add(root)
    try:
        session.commit()
    except IntegrityError as conflict:
        session.rollback()
        raise RootExists(path or root.path) from conflict
    session.refresh(root)
    return root


def delete(session: Session, root_id: str) -> bool:
    root = session.get(RootFolder, root_id)
    if root is None:
        return False
    session.delete(root)
    session.commit()
    return True


def record_verdicts(session: Session, verdicts: Mapping[str, tuple[str, str]], at: datetime | None = None) -> int:
    """Store what the gateway made of each root: ``{root id: (state, reason)}``. A verdict for a root that is not there
    (deleted meanwhile) is ignored. Returns how many roots changed state or reason. Committed."""
    checked = at or _now()
    changed = 0
    for root in session.exec(select(RootFolder).where(RootFolder.id.in_(list(verdicts)))).all():
        state, reason = verdicts[root.id]
        if (root.state, root.reason) != (state, reason):
            changed += 1
        root.state, root.reason, root.checked_at = state, reason, checked
        session.add(root)
    session.commit()
    return changed


# ------------------------------------------------------------------
# What the host remembers
# ------------------------------------------------------------------


def state_get(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(HostState, key)
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except ValueError:
        return default


def state_set(session: Session, key: str, value: Any, *, commit: bool = True) -> None:
    upsert(
        session, HostState(key=key, value=json.dumps(value, sort_keys=True), updated_at=_now()),
        conflict=("key",), update=("value", "updated_at"),
    )
    if commit:
        session.commit()


def state_delete(session: Session, key: str) -> None:
    row = session.get(HostState, key)
    if row is not None:
        session.delete(row)
        session.commit()


def paths_of(roots: Iterable[RootFolder]) -> list[tuple[str, str]]:
    """``(id, path)`` of each root, for ``core.folders.containing``."""
    return [(root.id, root.path) for root in roots]
