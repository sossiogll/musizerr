"""
What a release does to the database, for its ``release.json`` (ADR-0007 §4, §5.1).

Reads the migrations by their source (the standard library only, nothing of the application is imported: the
release workflow runs it on a bare runner) and prints a JSON object:

    {"contract_migrations": true|false, "contract": [...], "expand": [...], "irreversible": [...],
     "min_upgrade_from": "1.9.0"|null}

- ``--since REF`` limits ``contract``/``expand``/``irreversible`` to the migrations that are not in git ``REF``
  (the last release tag): what THIS release adds. Without it, every migration counts (the first release).
- ``contract_migrations`` is true when the release adds a contract migration: the update check
  (``core/versions.py``) then says it needs care.
- ``min_upgrade_from`` is ``core.schema_guard.MIN_UPGRADE_FROM``: the oldest version this one upgrades from.

A migration that declares no valid ``phase`` is an error (exit status 1): the same rule the test suite holds.

Usage, from the repository root: ``python3 app/scripts/migration_phases.py [--since v1.4.0]``.
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

APP = Path(__file__).resolve().parent.parent
VERSIONS = APP / "migrations" / "versions"
GUARD = APP / "core" / "schema_guard.py"
PHASES = ("expand", "contract")


def _assigned(tree: ast.Module, name: str) -> Optional[ast.expr]:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return node.value
    return None


def _constant(tree: ast.Module, name: str):
    value = _assigned(tree, name)
    return value.value if isinstance(value, ast.Constant) else None


def declared(source: str) -> dict:
    """``revision``, ``phase`` and ``irreversible`` as a migration module declares them."""
    tree = ast.parse(source)
    return {
        "revision": _constant(tree, "revision"),
        "phase": _constant(tree, "phase"),
        "irreversible": _constant(tree, "irreversible") is True,
    }


def migrations_here() -> dict[str, dict]:
    return {path.name: declared(path.read_text()) for path in sorted(VERSIONS.glob("*.py"))}


def names_at(ref: str) -> set[str]:
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, "--", "app/migrations/versions"],
        check=True, capture_output=True, text=True,
    ).stdout
    return {Path(line).name for line in listing.splitlines() if line.endswith(".py")}


def min_upgrade_from() -> Optional[str]:
    value = _constant(ast.parse(GUARD.read_text()), "MIN_UPGRADE_FROM")
    return value if isinstance(value, str) else None


def report(since: Optional[str] = None) -> dict:
    found = migrations_here()
    bad = sorted(name for name, m in found.items() if m["phase"] not in PHASES or not m["revision"])
    if bad:
        raise SystemExit("migrations without a valid phase (expand/contract): " + ", ".join(bad))
    old = names_at(since) if since else set()
    new = {name: m for name, m in found.items() if name not in old}
    contract = [m["revision"] for m in new.values() if m["phase"] == "contract"]
    return {
        "contract_migrations": bool(contract),
        "contract": contract,
        "expand": [m["revision"] for m in new.values() if m["phase"] == "expand"],
        "irreversible": [m["revision"] for m in new.values() if m["irreversible"]],
        "min_upgrade_from": min_upgrade_from(),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", help="a git ref (the last release tag): only the migrations added after it")
    args = parser.parse_args(argv)
    print(json.dumps(report(args.since)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
