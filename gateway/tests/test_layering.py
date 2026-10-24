"""The gateway is host infrastructure: the standard library and the SDK, never the application."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent / "musizeer_gateway"
APP_PACKAGES = {"core", "db", "services", "routes", "workers", "providers", "main"}
SDK_MODULES_ALLOWED = {"interfaces", "labels", "manifest", "permissions", "protocol", "wire", "failures"}


def imports():
    for path in sorted(GATEWAY.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield path, alias.name
            elif isinstance(node, ast.ImportFrom):
                yield path, ("." * node.level) + (node.module or "")


def test_there_are_sources_to_check():
    assert len(list(imports())) > 20


def test_the_gateway_imports_only_the_standard_library_the_sdk_and_itself():
    allowed = set(sys.stdlib_module_names) | {"musizeer_sdk", "musizeer_gateway"}
    offenders = [
        f"{path.name}: {name}" for path, name in imports()
        if not name.startswith(".") and name.split(".")[0] not in allowed
    ]
    assert not offenders, offenders


def test_the_gateway_never_imports_the_application():
    offenders = [f"{path.name}: {name}" for path, name in imports() if name.split(".")[0] in APP_PACKAGES]
    assert not offenders, offenders


def test_from_the_sdk_the_gateway_takes_the_protocol_the_manifest_and_their_types_only():
    offenders = []
    for path, name in imports():
        parts = name.split(".")
        if parts[0] == "musizeer_sdk" and len(parts) > 1 and parts[1] not in SDK_MODULES_ALLOWED:
            offenders.append(f"{path.name}: {name}")
    assert not offenders, offenders


def test_the_gateway_has_no_database_or_cache_client():
    forbidden = {"sqlalchemy", "sqlmodel", "redis", "celery", "psycopg", "alembic", "httpx", "requests"}
    assert not [name for _, name in imports() if name.split(".")[0] in forbidden]


def test_the_gateway_never_holds_the_key_of_the_stored_credentials():
    """It is given a source's token by the host, in memory, for a pull; it neither reads nor names the key that
    seals the credentials in the host's database (docs/credentials.md), and has no cryptography library."""
    offenders = [path.name for path in sorted(GATEWAY.rglob("*.py")) if "CREDENTIALS_KEY" in path.read_text(encoding="utf-8")]
    assert not offenders, offenders
    assert not [name for _, name in imports() if name.split(".")[0] == "cryptography"]


ENVIRONMENT_ACCESS = {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}


def environment_access(tree: ast.AST) -> list[str]:
    """Where a tree reads or writes the process environment: ``os.environ``, ``os.getenv``..., or a name taken from
    ``os`` by ``from os import environ``."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ENVIRONMENT_ACCESS and isinstance(node.value, ast.Name) and node.value.id == "os":
            found.append(f"os.{node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            found += [f"from os import {alias.name}" for alias in node.names if alias.name in ENVIRONMENT_ACCESS]
    return found


def test_only_the_environment_registry_reads_the_process_environment():
    """ADR-0009, rule 2: one reader per process, so the safety net for old names sees every variable."""
    offenders = []
    for path in sorted(GATEWAY.rglob("*.py")):
        if path.name == "environment.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [f"{path.name}: {what}" for what in environment_access(tree)]
    assert not offenders, offenders


def test_the_environment_scan_finds_what_it_looks_for():
    registry = ast.parse((GATEWAY / "environment.py").read_text(encoding="utf-8"))
    assert environment_access(registry), "the registry reads os.environ"
    spelled = ast.parse("import os\nfrom os import environ, path\nos.environ['A']\nos.getenv('B')")
    assert sorted(environment_access(spelled)) == ["from os import environ", "os.environ", "os.getenv"]
