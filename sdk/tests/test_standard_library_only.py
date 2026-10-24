"""
The SDK is what a provider installs and nothing else of Musizeer: its sources
import the standard library and themselves, and nothing else loads when they
are imported.
"""
from __future__ import annotations

import ast
import importlib.util
import pkgutil
import subprocess
import sys
import textwrap
from pathlib import Path

import musizeer_sdk

ROOT = Path(next(iter(musizeer_sdk.__path__)))
ALLOWED = set(sys.stdlib_module_names) | {"musizeer_sdk"}


def _imported_top_levels(path: Path) -> set[str]:
    tops: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            tops.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            tops.add((node.module or "").split(".")[0])
    return tops


def test_no_source_imports_anything_but_the_standard_library_and_the_sdk():
    sources = sorted(ROOT.rglob("*.py"))
    assert sources

    offenders = {str(p.relative_to(ROOT)): sorted(_imported_top_levels(p) - ALLOWED) for p in sources}

    assert {name: bad for name, bad in offenders.items() if bad} == {}


def test_importing_every_module_never_asks_for_anything_else():
    """Run in a fresh interpreter whose import system refuses everything that is
    neither the standard library nor the SDK."""
    modules = [m.name for m in pkgutil.walk_packages(musizeer_sdk.__path__, "musizeer_sdk.")]
    assert modules
    program = textwrap.dedent(
        f"""
        import importlib, importlib.abc, sys

        allowed = set(sys.stdlib_module_names) | {{"musizeer_sdk"}}

        class Refuse(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] not in allowed and not name.startswith("_"):
                    raise ImportError(f"not allowed: {{name}}")

        sys.meta_path.insert(0, Refuse())
        for name in {modules!r}:
            importlib.import_module(name)
        """
    )

    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_the_package_declares_no_dependency_and_a_typed_marker():
    import tomllib

    pyproject = tomllib.loads((ROOT.parent.parent / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["name"] == "musizeer-sdk"
    assert pyproject["project"]["dependencies"] == []
    assert pyproject["project"]["requires-python"] == ">=3.12"
    assert (ROOT / "py.typed").exists()
    assert importlib.util.find_spec("musizeer_sdk") is not None
