"""
Lo spec OpenAPI scritto a mano (``app/openapi.yaml``, servito da
``/openapi.yaml``) resta allineato alle rotte reali: ogni operazione
dell'app è documentata, nessuna voce documenta una rotta che non c'è più,
ogni riferimento si risolve e il file non ha chiavi duplicate.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

import main

SPEC_PATH = Path(main.__file__).resolve().parent / "openapi.yaml"
METHODS = {"get", "post", "put", "patch", "delete"}


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader che rifiuta le chiavi duplicate (safe_load terrebbe in
    silenzio l'ultima)."""


def _construct_mapping(loader, node, deep=False):
    keys = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in keys:
            raise AssertionError(f"chiave duplicata {key!r} alla riga {key_node.start_mark.line + 1}")
        keys.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _spec() -> dict:
    return yaml.load(SPEC_PATH.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)


def _operations(paths: dict) -> set[tuple[str, str]]:
    return {(method.upper(), path) for path, ops in paths.items() for method in ops if method in METHODS}


def test_every_route_is_documented_and_nothing_stale():
    routes = _operations(main.app.openapi()["paths"])
    documented = _operations(_spec()["paths"])

    assert sorted(routes - documented) == [], "rotte senza voce in app/openapi.yaml"
    assert sorted(documented - routes) == [], "voci di app/openapi.yaml per rotte che non esistono"


def test_every_reference_resolves():
    spec = _spec()
    missing = []

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/"):
                target = spec
                for part in ref[2:].split("/"):
                    target = target.get(part) if isinstance(target, dict) else None
                if target is None:
                    missing.append(ref)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(spec)
    assert missing == []


def test_operation_ids_are_unique():
    ids = [
        op["operationId"]
        for ops in _spec()["paths"].values()
        for method, op in ops.items()
        if method in METHODS and "operationId" in op
    ]
    assert len(ids) == len(set(ids))


def test_the_spec_is_served():
    resp = TestClient(main.app).get("/openapi.yaml")

    assert resp.status_code == 200
    assert resp.text.startswith("openapi:")


def test_download_states_match_the_domain():
    from core.download_states import ORDER

    assert _spec()["components"]["schemas"]["DownloadStatus"]["enum"] == list(ORDER)


def _dependencies(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _dependencies(dep)


def test_routes_authenticated_like_full_audio_declare_the_stream_cookie():
    from routes.deps import get_stream_user
    from tests.route_support import api_routes

    paths = _spec()["paths"]
    missing = [
        (method, route.path)
        for route in api_routes()
        if get_stream_user in set(_dependencies(route.dependant))
        for method in route.methods
        if {"StreamCookie": []} not in paths[route.path][method.lower()].get("security", [])
    ]
    assert missing == []


# ---------------------------------------------------------------------------
# Errori di autenticazione: ogni rotta che richiede un utente documenta il
# 401, ogni rotta admin (o che rifiuta un non-admin nel corpo) il 403, sempre
# con le risposte condivise di components/responses. La policy di ogni rotta
# viene dall'inventario di test_route_auth_inventory.py.
# ---------------------------------------------------------------------------

# Rotte non admin che rispondono 403 a un utente senza il ruolo necessario
# (controllo nel corpo della rotta, non una dipendenza).
_ROUTES_THAT_FORBID_IN_THE_BODY = {
    ("POST", "/auth/{provider}/test"),
    ("DELETE", "/auth/{provider}"),
    ("DELETE", "/auth/me/accounts"),
    ("PATCH", "/settings"),
    ("POST", "/auth/account/register"),
    ("POST", "/auth/account/login"),
}


def test_authenticated_routes_document_401_and_403_with_the_shared_responses():
    from tests.test_route_auth_inventory import EXPECTED

    problems = []
    for path, ops in _spec()["paths"].items():
        for method, op in ops.items():
            if method not in METHODS:
                continue
            key = (method.upper(), path)
            policy = EXPECTED[key]
            expected = {}
            if policy in ("user", "stream", "admin"):
                expected["401"] = "#/components/responses/Unauthorized"
            if policy == "admin" or key in _ROUTES_THAT_FORBID_IN_THE_BODY:
                expected["403"] = "#/components/responses/Forbidden"
            for code, ref in expected.items():
                documented = op["responses"].get(code)
                if documented != {"$ref": ref}:
                    problems.append(f"{method.upper()} {path}: {code} atteso {ref}, trovato {documented}")
    assert problems == []


def test_the_spec_uses_openapi_31_nullability():
    """``nullable: true`` è OpenAPI 3.0: in 3.1 (JSON Schema) non ha effetto,
    un campo che può essere null si dichiara con ``type: [X, "null"]``."""
    assert "nullable:" not in SPEC_PATH.read_text(encoding="utf-8")


def test_the_proxy_server_entry_is_under_the_public_prefix():
    servers = [s["url"] for s in _spec()["servers"]]

    assert "/api" in servers
    assert "/" not in servers
