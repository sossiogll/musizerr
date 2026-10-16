"""The gateway is host infrastructure: the standard library and the SDK, never the application."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent / "musizerr_gateway"
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
    allowed = set(sys.stdlib_module_names) | {"musizerr_sdk", "musizerr_gateway"}
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
        if parts[0] == "musizerr_sdk" and len(parts) > 1 and parts[1] not in SDK_MODULES_ALLOWED:
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
