"""
The product is called Musizeer (ADR-0010). Its old name, one letter apart, must not come back: a new
file, line or file name that spells it is a failure, except in the places that are history on purpose.

The old name is written in two halves here so that this file is not one of them. The one other place that
spells it is the safety net of ADR-0009: the registries of environment variables keep the OLD names of
the variables that were renamed, because a process that finds one set says so (``core/environment.py``,
``musizeer_gateway/environment.py``), and the guide prints them in its reference table
(``docs/deployment.md``, generated from the registry). Those names are listed, not matched by a pattern.
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
# The migrations that rewrite a stored value of the old name, and the test of the first (ADR-0010 row 21). 0046 (ADR-0008)
# sets the old working folder aside: the old default ended in a hidden folder named after the old product, and the migration
# must know that name to record it for the conversion.
MIGRATION_PATHS = (
    "app/migrations/versions/0044_rename_duplicate_origin.py",
    "app/tests/test_migration_0044_rename_duplicate_origin.py",
    "app/migrations/versions/0046_working_folder_legacy.py",
)
# The environment variables that carried the old product name, and the prefix itself (for the typo guard). They
# may be spelled in the registries of environment variables and in the guide's reference table, and nowhere else.
# Not a pattern: a new one is not allowed by being spelled like these.
OLD_ENVIRONMENT_NAMES = (
    OLD_ENV + "_VERSION",
    OLD_ENV + "_COMMIT",
    OLD_ENV + "_IMAGES",
    OLD_ENV + "_UPDATE_REPOSITORY",
)
OLD_ENVIRONMENT_PREFIX = OLD_ENV + "_"
SAFETY_NET_PATHS = (
    "app/core/environment.py",
    "gateway/musizeer_gateway/environment.py",
    "docs/deployment.md",
)

SKIPPED_DIRECTORIES = frozenset({
    ".git", "node_modules", "dist", ".pnpm-store", "__pycache__", ".venv", ".hypothesis", "build", ".vite",
    "test-results", "playwright-report", ".pytest_cache", ".mypy_cache", ".claude",
})
SKIPPED_SUFFIXES = (".egg-info",)

# The prefix alone is allowed only as the quoted constant of the typo guard; the four names, wherever the file may carry them.
_SAFETY_NET = re.compile("|".join(re.escape(name) + r"\b" for name in OLD_ENVIRONMENT_NAMES) + "|" + re.escape(f'"{OLD_ENVIRONMENT_PREFIX}"'))


def _files():
    for directory, names, files in os.walk(ROOT):
        names[:] = sorted(n for n in names if n not in SKIPPED_DIRECTORIES and not n.endswith(SKIPPED_SUFFIXES) and not (n == "data" and directory == str(ROOT)))
        for name in sorted(files):
            path = Path(directory) / name
            yield path, path.relative_to(ROOT).as_posix()


def _full(relative: str) -> str:
    prefix = "" if ROOT == REPOSITORY else "app/"  # the app's own container sees ``app/`` as its root
    return prefix + relative


def _is_history(relative: str) -> bool:
    full = _full(relative)
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
        safety_net = _full(relative) in SAFETY_NET_PATHS
        for number, line in enumerate(text.splitlines(), start=1):
            if OLD in (_SAFETY_NET.sub("", line) if safety_net else line).lower():
                found.append(f"{relative}:{number}: {line.strip()[:120]}")

    assert found == [], "the product is called Musizeer; these lines still use the old name:\n" + "\n".join(found[:40])


def test_the_old_environment_names_are_still_the_safety_nets():
    """The list only shrinks: a name that no registry keeps for the safety net is removed from it."""
    registry = (ROOT / ("core" if ROOT != REPOSITORY else "app/core") / "environment.py").read_text(encoding="utf-8")

    assert [name for name in OLD_ENVIRONMENT_NAMES if f'"{name}"' not in registry] == []
    assert f'"{OLD_ENVIRONMENT_PREFIX}"' in registry
