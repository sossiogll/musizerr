"""
Root folders and folder settings: the pure rules (ADR-0008).

A **root folder** is a location the administrator allows the product to use: a label and an absolute path as the containers
see it. It has no kind and no role. A **folder setting** (type ``folder``) is a root plus a RELATIVE path inside it; its
stored value is ``{"root_id": "<id>", "path": "<relative>"}``. The role (the working folder, a provider's library) belongs to
the setting that uses the root.

This module decides, without reading a database or a file system of its own:

- what a root's path may be (``root_path_problem``) and how a typed one is cleaned (``normalize_root_path``);
- the shape of a folder value (``parse_value``) and its relative path (``musizeer_sdk.folders``: the very rule a provider's
  suggested default is held to);
- the folder a value names (``resolve``): the root's path joined with the relative path, normalized. ``..`` is refused before
  anything is joined, and when the folder exists where the host runs, its real path must lie inside the real path of the root
  (a link that leaves the root is refused). The look at the file system is an argument (``real_path``), so a test can give a
  fake one;
- which root contains a given absolute path (``containing``), for the conversion of today's values.

``services/folders.py`` supplies the roots (the database) and the file system; ``services/roots.py`` the use cases.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from musizeer_sdk.folders import MAX_PATH, relative_path_problem

LABEL_MAX = 80

# The stable codes of a refusal (``FolderError.code``): the API answers with them and the interface words them.
ROOT_PATH_INVALID = "root_path_invalid"
ROOT_LABEL_INVALID = "root_label_invalid"
VALUE_INVALID = "folder_value_invalid"
PATH_INVALID = "folder_path_invalid"
ROOT_NOT_FOUND = "folder_root_not_found"
LEAVES_ROOT = "folder_leaves_root"
ROOT_REFUSED = "folder_root_refused"
NOT_MOUNTED = "folder_not_mounted"


class FolderError(ValueError):
    """A root or a folder value that cannot be used: ``code`` is the stable, translatable reason, ``message`` the text."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------


def inside(path: str, root: str) -> bool:
    """``path`` is ``root`` or lies under it (both absolute and normalized)."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def root_path_problem(path: object) -> str | None:
    """Why ``path`` cannot be the path of a root folder, or ``None``: text, at most 4096 characters, no NUL, absolute,
    normalized (no ``..``, ``//`` or trailing ``/``) and not ``/`` itself."""
    if not isinstance(path, str) or not path:
        return "a path is required"
    if len(path) > MAX_PATH or "\0" in path:
        return "is not a usable path"
    if not path.startswith("/"):
        return "must be absolute (it starts with '/')"
    segments = path.split("/")
    if "." in segments or ".." in segments:
        return "must not contain '.' or '..' segments"
    if posixpath.normpath(path) != path:
        return "must be normalized (no '//' and no trailing '/')"
    if path == "/":
        return "must not be '/' (a root is a folder of its own)"
    return None


def normalize_root_path(raw: object) -> str:
    """The path of a root from what was typed or picked: surrounding blanks and trailing ``/`` are dropped, anything else
    that makes it not a normalized absolute path is refused (``..`` is never silently collapsed)."""
    if not isinstance(raw, str):
        raise FolderError(ROOT_PATH_INVALID, "The path of a root folder is required")
    text = raw.strip()
    if len(text) > 1:
        text = text.rstrip("/") or "/"
    problem = root_path_problem(text)
    if problem is not None:
        raise FolderError(ROOT_PATH_INVALID, f"The path of a root folder {problem}")
    return text


def clean_label(raw: object, fallback: str = "") -> str:
    """The label of a root: trimmed, never empty, at most :data:`LABEL_MAX` characters."""
    text = raw.strip() if isinstance(raw, str) else ""
    text = text or fallback
    if not text:
        raise FolderError(ROOT_LABEL_INVALID, "A root folder needs a label")
    if len(text) > LABEL_MAX or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise FolderError(ROOT_LABEL_INVALID, f"The label of a root folder is at most {LABEL_MAX} characters, on one line")
    return text


def label_for(path: str) -> str:
    """The label a root gets when none is given: the last component of its path, capitalised (``/library`` -> ``Library``)."""
    name = posixpath.basename(path.rstrip("/")).lstrip(".")
    return (name[:1].upper() + name[1:]) if name else path


# ------------------------------------------------------------------
# Values
# ------------------------------------------------------------------


@dataclass(frozen=True)
class FolderValue:
    root_id: str
    path: str  # relative to the root; "" is the root itself

    def stored(self) -> dict[str, str]:
        return {"root_id": self.root_id, "path": self.path}


def is_unset(raw: object) -> bool:
    """None, an empty string and an empty object all mean "no folder"."""
    return raw is None or raw == "" or raw == {}


def parse_value(raw: object) -> FolderValue:
    """The value ``raw`` holds, or :class:`FolderError`. Exactly ``{"root_id": "<id>", "path": "<relative>"}``: no other
    key, the path relative and normalized (an absolute path is not a folder value: it is what the old settings held)."""
    if not isinstance(raw, Mapping) or set(raw) != {"root_id", "path"}:
        raise FolderError(VALUE_INVALID, 'A folder is {"root_id": "<root folder id>", "path": "<path inside it>"}')
    root_id, path = raw["root_id"], raw["path"]
    if not isinstance(root_id, str) or not root_id.strip() or len(root_id) > 64:
        raise FolderError(VALUE_INVALID, "A folder names its root folder by id")
    problem = relative_path_problem(path)
    if problem is not None:
        raise FolderError(PATH_INVALID, f"The path inside the root folder {problem}")
    return FolderValue(root_id, path)


def value_or_none(raw: object) -> FolderValue | None:
    """:func:`parse_value`, or ``None`` for an unset value or one that is not a folder value (a text an older version
    stored: for a reader that is "not set")."""
    if is_unset(raw):
        return None
    try:
        return parse_value(raw)
    except FolderError:
        return None


def join(root_path: str, relative: str) -> str:
    """The folder a relative path names under a root (the path is already known to be a clean relative one)."""
    return root_path if relative == "" else f"{root_path.rstrip('/')}/{relative}"


RealPath = Callable[[str], "str | None"]


def resolve(root_path: str, relative: str, real_path: RealPath | None = None) -> str:
    """The absolute, normalized folder of ``relative`` under the root at ``root_path``.

    ``..`` and every other irregular segment are refused (:class:`FolderError` ``folder_path_invalid``). ``real_path``
    answers "what is the real path of this, where the host runs?" (``None`` when it does not exist there): when the folder
    exists, its real path must lie inside the root's real path, or a link leaves the root (``folder_leaves_root``). A
    folder that does not exist yet cannot be a link, and the nearest folder that does is judged too, since a link on the way
    would lead out all the same."""
    problem = relative_path_problem(relative)
    if problem is not None:
        raise FolderError(PATH_INVALID, f"The path inside the root folder {problem}")
    folder = join(root_path, relative)
    if real_path is None:
        return folder
    real_root = real_path(root_path)
    if real_root is None:
        return folder  # the root is not where the host runs (the gateway alone sees it): nothing to compare with
    nearest = folder
    real_folder = real_path(nearest)
    while real_folder is None and nearest != root_path:
        nearest = posixpath.dirname(nearest)
        real_folder = real_path(nearest)
    if real_folder is not None and not inside(real_folder, real_root):
        raise FolderError(LEAVES_ROOT, "A link in the path leads out of the root folder")
    return folder


# ------------------------------------------------------------------
# Which root holds a path
# ------------------------------------------------------------------


def containing(roots: Iterable[tuple[str, str]], path: str) -> tuple[str, str] | None:
    """``(root id, relative path)`` for the DEEPEST of ``roots`` (pairs of ``(id, path)``) that contains the absolute
    ``path``; ``None`` when no root does."""
    best: tuple[str, str] | None = None
    best_length = -1
    for root_id, root_path in roots:
        if inside(path, root_path) and len(root_path) > best_length:
            relative = "" if path == root_path else path[len(root_path.rstrip("/")) + 1:]
            best, best_length = (root_id, relative), len(root_path)
    return best


def legacy_working_folder(value: str, old_leaf: str, new_leaf: str) -> tuple[str, str]:
    """``(parent folder, name)`` a stored working folder becomes (ADR-0008, the conversion): a value that ends in the old
    hidden name (``old_leaf``, recorded by migration 0046) keeps its parent and gets the new name (the folder is ephemeral, the old one is left on disk); any other value
    keeps its parent and its last component, the same folder. :class:`FolderError` when the value is not a path whose parent
    can be a root (``/downloads`` has the parent ``/``, which is not a folder)."""
    problem = root_path_problem(value.rstrip("/") or "/")
    if problem is not None:
        raise FolderError(ROOT_PATH_INVALID, f"The stored working folder {problem}")
    cleaned = value.rstrip("/")
    parent, name = posixpath.split(cleaned)
    if parent in ("", "/"):
        raise FolderError(ROOT_PATH_INVALID, f"'{value}' is a folder of '/': no root folder can hold it")
    return parent, new_leaf if name == old_leaf else name
