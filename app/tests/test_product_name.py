"""
The product is called Musizeer (ADR-0010). Its old name, one letter apart, must not come back: a new
file, line or file name that spells it is a failure, except in the places that are history on purpose.

The old name is written in two halves here so that this file is not one of them. Only the environment
variable names the next block renames (ADR-0009) are left, and they are listed, not matched by a pattern.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

OLD = "musiz" + "err"
OLD_ENV = OLD.upper()

REPOSITORY = Path(__file__).resolve().parents[2]
# Inside the application's own container only ``app/`` is there; the checkout has the rest.
ROOT = REPOSITORY if (REPOSITORY / "sdk").is_dir() and (REPOSITORY / "gateway").is_dir() else Path(__file__).resolve().parents[1]

# History: the records written before the rename keep the name they were written under (ADR-0010), and
# the ones that describe the rename (0008 to 0010) name the old one. The index says so too.
HISTORY_PATHS = (
    "docs/adr/0001-", "docs/adr/0002-", "docs/adr/0003-", "docs/adr/0004-", "docs/adr/0005-", "docs/adr/0006-",
    "docs/adr/0007-", "docs/adr/0008-", "docs/adr/0009-", "docs/adr/0010-", "docs/adr/README.md",
)
# The migration that rewrites the stored value of the old name, and the test of it (ADR-0010 row 21).
MIGRATION_PATHS = (
    "app/migrations/versions/0044_rename_duplicate_origin.py",
    "app/tests/test_migration_0044_rename_duplicate_origin.py",
)
# The environment variable names left for the block that renames them (ADR-0009). Not a pattern: a new
# one is not allowed by being spelled like these.
RESERVED_ENVIRONMENT_NAMES = (
    OLD_ENV + "_VERSION",
    OLD_ENV + "_COMMIT",
    OLD_ENV + "_IMAGES",
    OLD_ENV + "_UPDATE_REPOSITORY",
)

SKIPPED_DIRECTORIES = frozenset({
    ".git", "node_modules", "dist", ".pnpm-store", "__pycache__", ".venv", ".hypothesis", "build", ".vite",
    "test-results", "playwright-report", ".pytest_cache", ".mypy_cache", ".claude",
})
SKIPPED_SUFFIXES = (".egg-info",)

_RESERVED = re.compile("|".join(re.escape(name) for name in RESERVED_ENVIRONMENT_NAMES))


def _files():
    for directory, names, files in os.walk(ROOT):
        names[:] = sorted(n for n in names if n not in SKIPPED_DIRECTORIES and not n.endswith(SKIPPED_SUFFIXES) and not (n == "data" and directory == str(ROOT)))
        for name in sorted(files):
            path = Path(directory) / name
            yield path, path.relative_to(ROOT).as_posix()


def _is_history(relative: str) -> bool:
    prefix = "" if ROOT == REPOSITORY else "app/"  # the app's own container sees ``app/`` as its root
    full = prefix + relative
    return full.startswith(HISTORY_PATHS) or full in MIGRATION_PATHS


def test_no_path_carries_the_old_name():
    found = [relative for _, relative in _files() if OLD in relative.lower() and not _is_history(relative)]

    assert found == [], "a file or folder is still named after the old product"


def test_no_line_carries_the_old_name_outside_history():
    found: list[str] = []
    for path, relative in _files():
        if _is_history(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # a binary file
        if OLD not in text.lower():
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if OLD in _RESERVED.sub("", line).lower():
                found.append(f"{relative}:{number}: {line.strip()[:120]}")

    assert found == [], "the product is called Musizeer; these lines still use the old name:\n" + "\n".join(found[:40])


def test_the_reserved_environment_names_are_still_in_use():
    """An entry nobody reads any more is removed from the list, so the list only shrinks."""
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path, relative in _files()
        if relative.endswith((".py", ".yml", ".yaml", ".ts", ".tsx", ".sh", "Dockerfile"))
    )

    assert [name for name in RESERVED_ENVIRONMENT_NAMES if name not in text] == []
