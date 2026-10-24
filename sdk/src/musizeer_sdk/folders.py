"""
The path inside a root folder: the one rule of what a ``folder`` setting may hold (ADR-0008).

A ``folder`` setting is a root folder plus a RELATIVE path. The host resolves it and the provider receives the absolute
result; the same rule decides what a provider may suggest as the ``default`` of its own folder setting
(``musizeer_sdk.conformance``) and what the host accepts as the path of a value (``app/core/folders.py``), so a
suggestion that the host would refuse can never be declared.

Standard library only, pure text: no file is looked at.
"""
from __future__ import annotations

MAX_PATH = 4096


def relative_path_problem(path: object) -> str | None:
    """Why ``path`` cannot be a path inside a root folder, or ``None`` when it can.

    A usable path is text of at most :data:`MAX_PATH` characters with no NUL, no leading ``/`` and no empty, ``.`` or
    ``..`` segment (``a//b``, ``./a`` and ``a/../b`` are refused, so is a trailing ``/``). The empty string is the root
    itself and is usable. Backslashes are ordinary characters: a name may contain one on a Unix file system."""
    if not isinstance(path, str):
        return "is not text"
    if len(path) > MAX_PATH:
        return f"is longer than {MAX_PATH} characters"
    if "\0" in path:
        return "contains a NUL character"
    if path == "":
        return None
    if path.startswith("/"):
        return "is absolute (it must be relative to the root folder)"
    for part in path.split("/"):
        if part == "":
            return "has an empty segment ('//' or a trailing '/')"
        if part == ".":
            return "has a '.' segment"
        if part == "..":
            return "has a '..' segment (a folder cannot leave its root)"
    return None


def is_relative_path(path: object) -> bool:
    return relative_path_problem(path) is None
