"""
Guardia strutturale della direzione delle dipendenze fra i package
dell'app: ``routes``/``workers`` -> ``services`` -> ``core``/``db`` e
``core`` -> ``musizeer_sdk`` (il contratto dei provider, in ``sdk/``);
l'SDK dipende solo dalla libreria standard. Nessun provider vive in questo
repository: l'host li raggiunge solo dal gateway (regola 6). Cammina ogni file
``.py`` sotto ``app/`` con
``ast`` (import a livello di modulo E dentro funzioni, risolvendo gli
import relativi al modulo assoluto), non un semplice grep — cosi' un
import annidato in una funzione non passa inosservato.

Se una regola qui sotto fallisce, il fix e' migrare la violazione al layer
giusto (o aggiungerla all'allowlist esplicita, se e' un caso già accettato
e documentato), mai allargare la regola.
"""
from __future__ import annotations

import ast
import os
from collections import defaultdict

import pytest

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directory escluse dalla scansione: test, migrazioni Alembic (storiche,
# non soggette alla policy di layering corrente) e script one-off.
EXCLUDE_DIRS = {"tests", "migrations", "scripts", "__pycache__"}

TOP_PACKAGES = {"core", "db", "services", "routes", "workers"}


def _iter_py_files():
    for dirpath, dirnames, filenames in os.walk(APP_ROOT):
        rel = os.path.relpath(dirpath, APP_ROOT)
        parts = [] if rel == "." else rel.split(os.sep)
        if parts and parts[0] in EXCLUDE_DIRS:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
        # Solo i sei package soggetti alla policy: al primo livello, non file sciolti come main.py.
        if not parts or parts[0] not in TOP_PACKAGES:
            continue
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _module_name_for_file(path: str) -> str:
    rel = os.path.relpath(path, APP_ROOT)
    if rel.endswith("__init__.py"):
        rel = rel[: -len("/__init__.py")]
    else:
        rel = rel[: -len(".py")]
    return rel.replace(os.sep, ".")


def _resolve_relative(module_name: str, is_package: bool, level: int, module: str | None) -> str:
    """Risolve un import relativo (``from . import x`` / ``from ..a import
    b``) al nome di modulo assoluto, a partire dal package del file che lo
    contiene: per un ``__init__.py`` e' il modulo stesso, altrimenti il
    modulo senza l'ultimo componente."""
    package_parts = module_name.split(".") if is_package else module_name.split(".")[:-1]
    if level > 1:
        cut = level - 1
        package_parts = package_parts[:-cut] if len(package_parts) >= cut else []
    parts = list(package_parts)
    if module:
        parts += module.split(".")
    return ".".join(parts)


class Violation:
    __slots__ = ("file", "lineno", "target")

    def __init__(self, file: str, lineno: int, target: str) -> None:
        self.file = file
        self.lineno = lineno
        self.target = target

    def __str__(self) -> str:
        return f"{self.file}:{self.lineno} -> {self.target}"


def _collect_imports() -> dict[str, list[Violation]]:
    """{package_sorgente: [Violation, ...]} — per ogni file, OGNI import
    (di modulo o annidato in una funzione) il cui top-level package fra i
    sei soggetti alla policy sia DIVERSO dal package del file stesso."""
    by_src: dict[str, list[Violation]] = defaultdict(list)
    for path in _iter_py_files():
        rel = os.path.relpath(path, APP_ROOT)
        src_top = rel.split(os.sep)[0]
        module_name = _module_name_for_file(path)
        with open(path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in TOP_PACKAGES and top != src_top:
                        by_src[src_top].append(Violation(rel, node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    full = _resolve_relative(
                        module_name, path.endswith("__init__.py"), node.level, node.module,
                    )
                else:
                    full = node.module or ""
                top = full.split(".")[0] if full else ""
                if top in TOP_PACKAGES and top != src_top:
                    by_src[src_top].append(Violation(rel, node.lineno, full))
    return by_src


@pytest.fixture(scope="module")
def imports_by_package() -> dict[str, list[Violation]]:
    return _collect_imports()


def _fail_on(violations: list[Violation], rule: str) -> None:
    if violations:
        listing = "\n".join(f"  {v}" for v in violations)
        pytest.fail(f"{rule}:\n{listing}")


# ---------------------------------------------------------------------------
# Regola 1 — core non importa services/routes/workers.
# ---------------------------------------------------------------------------

def test_core_does_not_import_services_routes_workers(imports_by_package):
    offenders = [
        v for v in imports_by_package.get("core", [])
        if v.target.split(".")[0] in {"services", "routes", "workers"}
    ]
    _fail_on(offenders, "core importa services/routes/workers")



# ---------------------------------------------------------------------------
# Regola 2 — core non importa db: il dominio e il contratto dei provider non
# conoscono la persistenza dell'host.
# ---------------------------------------------------------------------------

def test_core_does_not_import_db(imports_by_package):
    offenders = [v for v in imports_by_package.get("core", []) if v.target.split(".")[0] == "db"]
    _fail_on(offenders, "core importa db")


# ---------------------------------------------------------------------------
# Regola 3 — services non importa routes; da workers importa SOLO
# workers.celery_app.
# ---------------------------------------------------------------------------

def test_services_does_not_import_routes(imports_by_package):
    offenders = [v for v in imports_by_package.get("services", []) if v.target.split(".")[0] == "routes"]
    _fail_on(offenders, "services importa routes")


def test_services_imports_only_workers_celery_app(imports_by_package):
    offenders = [
        v for v in imports_by_package.get("services", [])
        if v.target.split(".")[0] == "workers" and v.target != "workers.celery_app"
        and not v.target.startswith("workers.celery_app.")
    ]
    _fail_on(offenders, "services importa da workers un modulo diverso da workers.celery_app")


# ---------------------------------------------------------------------------
# Regola 4 — workers non importa routes.
# ---------------------------------------------------------------------------

def test_workers_does_not_import_routes(imports_by_package):
    offenders = [v for v in imports_by_package.get("workers", []) if v.target.split(".")[0] == "routes"]
    _fail_on(offenders, "workers importa routes")


# ---------------------------------------------------------------------------
# Regola 5 — db non importa services/routes/workers.
# ---------------------------------------------------------------------------

def test_db_does_not_import_services_routes_workers(imports_by_package):
    offenders = [
        v for v in imports_by_package.get("db", [])
        if v.target.split(".")[0] in {"services", "routes", "workers"}
    ]
    _fail_on(offenders, "db importa services/routes/workers")


# ---------------------------------------------------------------------------
# Regola 6 — nessun provider vive in questo repository: i provider sono
# pacchetti a sé (altri repository, ciascuno con i propri test e la propria
# immagine) e l'host li raggiunge solo da ciò che il gateway fa girare
# (``services/provider_registry.py``). Né un package ``providers`` sotto
# ``app/``, né un import di uno dei suoi moduli da qualunque file di ``app/``
# (test compresi).
# ---------------------------------------------------------------------------


def test_no_provider_package_lives_in_the_application():
    assert not os.path.exists(os.path.join(APP_ROOT, "providers")), (
        "app/providers/ non esiste più: un provider è un pacchetto a sé, mai codice dell'host"
    )


def test_nothing_in_the_application_imports_a_provider_module():
    offenders = []
    for dirpath, dirnames, filenames in os.walk(APP_ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__" and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, APP_ROOT)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=rel)
            module_name = _module_name_for_file(path) if rel.split(os.sep)[0] in TOP_PACKAGES else rel[:-3].replace(os.sep, ".")
            for node in ast.walk(tree):
                for target in _absolute_targets(node, module_name, fn == "__init__.py"):
                    if target.split(".")[0] == "providers":
                        offenders.append(Violation(rel, node.lineno, target))
    _fail_on(offenders, "un file dell'applicazione importa un modulo di provider")


def _absolute_targets(node, module_name, is_package):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level:
            return [_resolve_relative(module_name, is_package, node.level, node.module)]
        return [node.module or ""]
    return []




# ---------------------------------------------------------------------------
# Regola 9 — l'SDK importa solo la libreria standard e se stesso: niente host,
# niente provider, niente libreria di terze parti. Si cammina sui sorgenti
# reali del package installato (in sviluppo e nei test è ``sdk/src``).
# ---------------------------------------------------------------------------


def _sdk_imports() -> list[tuple[str, str, int]]:
    """(file, modulo importato, riga) per ogni import dei sorgenti dell'SDK."""
    import importlib.util

    spec = importlib.util.find_spec("musizeer_sdk")
    assert spec is not None and spec.submodule_search_locations, "musizeer_sdk non è installato"
    root = list(spec.submodule_search_locations)[0]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root)
            parts = ["musizeer_sdk", *rel[: -len(".py")].split(os.sep)]
            is_package = parts[-1] == "__init__"
            module_name = ".".join(parts[:-1] if is_package else parts)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=rel)
            for node in ast.walk(tree):
                for target in _absolute_targets(node, module_name, is_package):
                    if target:
                        found.append((rel, target, node.lineno))
    return found


def test_the_sdk_has_sources_to_check():
    imports = _sdk_imports()
    assert any(target.split(".")[0] == "musizeer_sdk" for _rel, target, _lineno in imports), (
        "la regola 9 passerebbe a vuoto: nessun import dell'SDK su se stesso trovato"
    )


def test_the_sdk_imports_only_the_standard_library_and_itself():
    import sys

    allowed = set(sys.stdlib_module_names) | {"musizeer_sdk"}
    offenders = [
        Violation(rel, lineno, target)
        for rel, target, lineno in _sdk_imports()
        if target.split(".")[0] not in allowed
    ]
    _fail_on(offenders, "l'SDK importa qualcosa che non è la libreria standard né se stesso")


# ---------------------------------------------------------------------------
# Regola 10 — l'host raggiunge un provider SOLO tramite il protocollo di
# chiamata. Il runner (che costruisce il provider e lo tiene) vive dal lato del
# provider: nessun modulo dell'host lo importa, nemmeno per i test (la loro
# infrastruttura è ``tests/loopback.py``, che porta un provider dei test al
# proxy attraverso lo stesso protocollo). Tutto il resto riceve proxy
# (services/provider_proxy.py).
# ---------------------------------------------------------------------------

def _host_importers(prefixes: tuple[str, ...]) -> set[str]:
    """I file dell'host che importano uno dei moduli ``prefixes`` (o un nome da
    uno di essi: ``from musizeer_sdk import runner``)."""
    found = set()
    for path in _iter_py_files():
        rel = os.path.relpath(path, APP_ROOT)
        module_name = _module_name_for_file(path)
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=rel)
        for node in ast.walk(tree):
            targets = _absolute_targets(node, module_name, path.endswith("__init__.py"))
            if isinstance(node, ast.ImportFrom):
                targets = [f"{target}.{alias.name}" for target in targets for alias in node.names] + targets
            if any(t == p or t.startswith(p + ".") for t in targets for p in prefixes):
                found.add(rel)
    return found




def test_the_host_never_imports_the_runner():
    assert _host_importers(("musizeer_sdk.runner",)) == set()


def test_the_host_never_imports_the_loopback_transport_of_the_tests():
    assert _host_importers(("tests",)) == set()


def test_the_scan_finds_the_importers_it_expects():
    # The scan would pass on an empty tree: it must find the importers it expects.
    assert _host_importers(("musizeer_sdk.protocol",)) >= {os.path.join("services", "provider_proxy.py")}



# ---------------------------------------------------------------------------
# Regola 11 — i provider che il gateway fa girare li raggiunge solo il registry,
# col trasporto su socket; e il gateway è
# infrastruttura a sé: l'applicazione non importa MAI il suo package (parlano
# col socket di controllo), e il gateway non importa mai l'applicazione
# (``gateway/tests/test_layering.py`` lo verifica dal suo lato).
# ---------------------------------------------------------------------------

SOCKET_USERS = {os.path.join("services", "provider_registry.py")}


def test_only_the_registry_imports_the_socket_transport():
    assert _host_importers(("services.provider_socket",)) == SOCKET_USERS


def test_the_application_never_imports_the_gateway():
    assert _host_importers(("musizeer_gateway",)) == set()


# ---------------------------------------------------------------------------
# Regola 12 — la chiave delle credenziali (docs/credentials.md). Il FORMATO e la
# crittografia sono ``core/secret_box.py``: puro, riceve le chiavi come
# argomenti, non legge l'ambiente. L'AMBIENTE lo legge UN solo modulo per
# processo, ``core/environment.py`` (il registro, ADR-0009), e i NOMI delle
# variabili della chiave (MUSIZEER_CREDENTIALS_KEY e _PREVIOUS, con i vecchi
# nomi che il registro tiene per il messaggio di avvio) li scrivono solo il
# registro e ``db/credential_cipher.py``: nessun altro file dell'host ne scrive
# il nome, quindi nessuno può leggere la chiave per conto suo, e il gateway
# (che non importa nulla dell'applicazione) non la riceve mai.
# ---------------------------------------------------------------------------

KEY_VARIABLES = {
    "MUSIZEER_CREDENTIALS_KEY", "MUSIZEER_CREDENTIALS_KEY_PREVIOUS",
    "CREDENTIALS_KEY", "CREDENTIALS_KEY_PREVIOUS",
}
ENVIRONMENT_REGISTRY = os.path.join("core", "environment.py")
CIPHER = os.path.join("db", "credential_cipher.py")
KEY_NAMERS = {ENVIRONMENT_REGISTRY, CIPHER}


def _application_sources():
    for dirpath, dirnames, filenames in os.walk(APP_ROOT):
        rel = os.path.relpath(dirpath, APP_ROOT)
        if rel.split(os.sep)[0] == "tests":
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d != "__pycache__" and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                path = os.path.join(dirpath, fn)
                with open(path, encoding="utf-8") as f:
                    yield os.path.relpath(path, APP_ROOT), ast.parse(f.read(), filename=path)


def test_only_the_registry_and_the_credential_cipher_name_the_key_variables():
    offenders = [
        Violation(rel, node.lineno, node.value)
        for rel, tree in _application_sources() if rel not in KEY_NAMERS
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in KEY_VARIABLES
    ]
    _fail_on(offenders, "un modulo diverso da core/environment.py e db/credential_cipher.py nomina la variabile della chiave delle credenziali")


def test_the_key_variables_are_named_where_the_scan_expects_them():
    owners = {
        rel for rel, tree in _application_sources()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in KEY_VARIABLES
    }
    assert owners == KEY_NAMERS


def test_the_secret_box_is_pure():
    with open(os.path.join(APP_ROOT, "core", "secret_box.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported = {
        (alias.name if isinstance(node, ast.Import) else node.module).split(".")[0]
        for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [node])
    }
    assert imported <= {"__future__", "base64", "binascii", "typing", "cryptography"}, imported
    assert "os" not in imported, "the keys come in as arguments: the environment is core/environment.py's"


# ---------------------------------------------------------------------------
# Regola 14 — UN lettore dell'ambiente per processo (ADR-0009, regola 2). Solo
# ``core/environment.py`` tocca ``os.environ`` / ``os.getenv``: nomi, default e
# obbligatorieta' stanno nel suo registro, e l'avviso su un vecchio nome o su un
# refuso (``MUSIZEER_AUTH_SECERT``) esiste proprio perche' tutto passa da li.
# Vale per ogni file sotto ``app/`` tranne i test (migrazioni e script inclusi).
# ---------------------------------------------------------------------------

ENVIRONMENT_ACCESS = {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}


def environment_access(tree: ast.AST) -> list[tuple[int, str]]:
    """Where a tree reads or writes the process environment: ``os.environ``, ``os.getenv``..., or a name taken
    from ``os`` by ``from os import environ``."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ENVIRONMENT_ACCESS and isinstance(node.value, ast.Name) and node.value.id == "os":
            found.append((node.lineno, f"os.{node.attr}"))
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            found += [(node.lineno, f"from os import {alias.name}") for alias in node.names if alias.name in ENVIRONMENT_ACCESS]
    return found


def test_only_the_environment_registry_reads_the_process_environment():
    offenders = [
        Violation(rel, line, what)
        for rel, tree in _application_sources() if rel != ENVIRONMENT_REGISTRY
        for line, what in environment_access(tree)
    ]
    _fail_on(offenders, "solo core/environment.py legge l'ambiente del processo: aggiungi la variabile al registro")


def test_the_environment_scan_finds_what_it_looks_for():
    """The scan is not vacuous: the registry itself reads the environment, and the detector sees every spelling."""
    registry = dict(_application_sources())[ENVIRONMENT_REGISTRY]
    assert environment_access(registry), "the registry reads os.environ"
    spelled = ast.parse("import os\nfrom os import environ, getenv, path\nos.environ['A']\nos.getenv('B')\nos.putenv('C', 'D')")
    assert [what for _, what in environment_access(spelled)] == [
        "from os import environ", "from os import getenv", "os.environ", "os.getenv", "os.putenv",
    ]


# ---------------------------------------------------------------------------
# Regola 13 — gli account di un utente li instrada una regola sola
# (``db/auth_store.CredentialStore``) e CHI e' il proprietario dell'istanza lo
# decide un solo punto, ``services/credentials.py`` (solo con l'autenticazione
# spenta l'utente di sistema ha come account personale quello condiviso). Nessun
# altro modulo costruisce uno store per un utente (``user_id``, ``own_only``,
# ``instance_owner``): usa ``user_store`` / ``own_store`` / ``shared_store``.
# ---------------------------------------------------------------------------

STORE_BUILDER = os.path.join("services", "credentials.py")
PRINCIPAL_ARGUMENTS = {"user_id", "own_only", "instance_owner"}


def test_only_services_credentials_builds_a_store_for_a_user():
    offenders = []
    for rel, tree in _application_sources():
        if rel == STORE_BUILDER:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name != "CredentialStore":
                continue
            if len(node.args) > 1 or any(keyword.arg in PRINCIPAL_ARGUMENTS for keyword in node.keywords):
                offenders.append(Violation(rel, node.lineno, "CredentialStore(...) for a principal"))
    _fail_on(offenders, "uno store di un utente va costruito con services.credentials (user_store/own_store)")
