"""
The environment of Musizeer (ADR-0009): ONE registry of every variable, ONE reader per process.

What is checked here, besides the registry itself:

- the safety net: an old name that is still set is reported with the name that replaces it (never with its value), a
  required variable that is unset fails the start with the same hint, a mistyped ``MUSIZEER_`` variable gets a suggestion;
- the registry of the gateway (it cannot import this one) declares the same entries, with the same defaults;
- everything that names a variable agrees with the registry: the compose files, ``.env.example``, the reference table of
  ``docs/deployment.md`` (generated from it), the web app's image.
"""
from __future__ import annotations

import importlib.util
import os
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest


from core import environment, user_auth
from db import credential_cipher
from tests.credentials_support import KEY
from tests.test_auth_secret_startup import _run_lifespan

APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parent
COMPOSE_FILES = ("docker-compose.yaml", "docker-compose.dev.yml", "docker-compose.build.yml")

STANDARD_NAMES = {
    "DATABASE_URL", "REDIS_URL", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "E2E_USERNAME", "E2E_PASSWORD",
    "E2E_ADMIN_USERNAME", "E2E_ADMIN_PASSWORD",
}


def checkout(*parts: str) -> Path:
    """A file of the repository; the test is skipped where the application runs without the rest of it."""
    path = REPO_ROOT.joinpath(*parts)
    if not path.exists():
        pytest.skip(f"{'/'.join(parts)} is not next to the application (it is not a checkout)")
    return path


def gateway_registry():
    """The gateway's registry, loaded from its file (the application never imports the gateway; it has only the standard
    library behind it, so it loads on its own)."""
    name = "gateway_environment_under_test"
    spec = importlib.util.spec_from_file_location(name, checkout("gateway", "musizeer_gateway", "environment.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # a dataclass looks its module up here
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_every_name_is_unique_and_either_prefixed_or_a_standard_one():
    names = [entry.name for entry in environment.ENTRIES]

    assert len(names) == len(set(names))
    assert [n for n in names if not n.startswith(environment.PREFIX) and n not in STANDARD_NAMES] == []


def test_the_prefixed_names_are_the_ones_the_product_defines():
    """A standard name never gets the prefix (ADR-0009, section 2), and the product's own always does."""
    assert {n for n in STANDARD_NAMES if n.startswith(environment.PREFIX)} == set()
    assert not [e.name for e in environment.ENTRIES if e.name in {"AUTH_SECRET", "DATA_DIR", "APP_PORT"}]


def test_an_old_name_is_never_a_new_name_and_never_a_prefixed_one():
    for entry in environment.ENTRIES:
        assert entry.old_name != entry.name
        assert entry.old_name is None or not entry.old_name.startswith(environment.PREFIX), entry.name
        assert entry.old_name is None or entry.old_name not in environment.BY_NAME, entry.name


def test_every_entry_is_well_formed():
    for entry in environment.ENTRIES:
        assert entry.group in environment.GROUPS, entry.name
        assert entry.readers and set(entry.readers) <= {environment.APP, environment.GATEWAY, environment.COMPOSE, environment.FRONTEND}
        assert entry.meaning.strip().endswith("."), entry.name
        assert not (entry.required and entry.required_when), entry.name
        assert not (entry.required and entry.default), f"{entry.name}: a required variable has no default"
        if entry.required:
            assert entry.group == environment.REQUIRED, entry.name


def test_the_secrets_are_declared_secret():
    secret = {e.name for e in environment.ENTRIES if e.secret}

    assert {
        "MUSIZEER_CREDENTIALS_KEY", "MUSIZEER_CREDENTIALS_KEY_PREVIOUS", "MUSIZEER_AUTH_SECRET", "MUSIZEER_ADMIN_BOOTSTRAP_PASSWORD",
        "POSTGRES_PASSWORD", "DATABASE_URL",
    } <= secret


def test_the_two_variables_the_process_cannot_start_without_are_the_required_ones():
    assert environment.BY_NAME["MUSIZEER_CREDENTIALS_KEY"].required
    assert environment.BY_NAME["MUSIZEER_AUTH_SECRET"].required_when == "authentication is on"
    assert environment.BY_NAME["POSTGRES_PASSWORD"].required


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_a_value_wins_over_the_default_and_an_empty_one_is_no_value():
    assert environment.get("MUSIZEER_DATA_DIR", {}) == "/data"
    assert environment.get("MUSIZEER_DATA_DIR", {"MUSIZEER_DATA_DIR": "/elsewhere"}) == "/elsewhere"
    assert environment.get("MUSIZEER_DATA_DIR", {"MUSIZEER_DATA_DIR": ""}) == "/data", "compose passes an empty value for an unset variable"


def test_the_old_name_is_never_read_as_the_value():
    assert environment.get("MUSIZEER_DATA_DIR", {"DATA_DIR": "/old"}) == "/data"
    assert environment.get("MUSIZEER_CREDENTIALS_KEY", {"CREDENTIALS_KEY": "old-key"}) == ""


def test_a_variable_the_application_does_not_read_is_refused():
    with pytest.raises(environment.NotRead):
        environment.get("MUSIZEER_PORT", {})  # compose's
    with pytest.raises(environment.NotRead):
        environment.get("MUSIZEER_GATEWAY_STATE_DIR", {})  # the gateway's
    with pytest.raises(environment.NotRead):
        environment.get("NOT_A_VARIABLE", {})


def test_a_number_that_is_not_one_falls_back_to_the_default():
    assert environment.get_int("MUSIZEER_DB_POOL_SIZE", {"MUSIZEER_DB_POOL_SIZE": "7"}) == 7
    assert environment.get_int("MUSIZEER_DB_POOL_SIZE", {"MUSIZEER_DB_POOL_SIZE": "many"}) == 5
    assert environment.get_int("MUSIZEER_DB_POOL_SIZE", {"MUSIZEER_DB_POOL_SIZE": ""}) == 5


def test_the_callback_and_the_landing_page_derive_from_the_public_url(monkeypatch):
    for name in ("MUSIZEER_PUBLIC_URL", "MUSIZEER_APP_BASE_URL", "MUSIZEER_FRONTEND_URL"):
        monkeypatch.delenv(name, raising=False)
    assert (environment.app_base_url(), environment.frontend_url()) == ("http://localhost/api", "http://localhost")

    monkeypatch.setenv("MUSIZEER_PUBLIC_URL", "https://music.example.org/")
    assert (environment.app_base_url(), environment.frontend_url()) == ("https://music.example.org/api", "https://music.example.org")

    monkeypatch.setenv("MUSIZEER_APP_BASE_URL", "http://api.internal:8000/")
    monkeypatch.setenv("MUSIZEER_FRONTEND_URL", "")
    assert (environment.app_base_url(), environment.frontend_url()) == ("http://api.internal:8000", "https://music.example.org"), \
        "the derived forms are overridable; an empty override is none"


# ---------------------------------------------------------------------------
# The safety net: old names
# ---------------------------------------------------------------------------

CANARY = "canary-value-that-must-never-be-printed"


def test_an_old_name_that_is_set_is_an_error_naming_the_new_one():
    findings = environment.check({"AUTH_SECRET": CANARY, "CREDENTIALS_KEY": CANARY, "DATA_DIR": "/old"})

    assert sorted(findings) == [
        ("error", "AUTH_SECRET is no longer read: rename it to MUSIZEER_AUTH_SECRET"),
        ("error", "CREDENTIALS_KEY is no longer read: rename it to MUSIZEER_CREDENTIALS_KEY"),
        ("error", "DATA_DIR is no longer read: rename it to MUSIZEER_DATA_DIR"),
    ]
    assert CANARY not in str(findings) and "/old" not in str(findings), "a value is never printed"


@pytest.mark.parametrize("entry", [e for e in environment.ENTRIES if e.old_name and environment.APP in e.readers], ids=lambda e: e.name)
def test_every_old_name_the_application_read_is_reported(entry):
    assert environment.check({entry.old_name: "x"}) == [("error", f"{entry.old_name} is no longer read: rename it to {entry.name}")]


def test_an_old_name_that_is_empty_or_absent_is_not_reported():
    assert environment.check({"AUTH_SECRET": ""}) == []
    assert environment.check({}) == []


def test_the_old_product_prefix_is_named():
    old = environment.OLD_PREFIX

    assert environment.check({old + "VERSION": "1.0"}) == [("error", f"{old}VERSION is no longer read: rename it to MUSIZEER_VERSION")]
    assert environment.check({old + "IMAGES": "x"}) == [("warning", f"{old}IMAGES is not read; did you mean MUSIZEER_IMAGES?")], \
        "compose's variable: no process reads it, but the mistake is the same"
    assert environment.check({old + "WHATEVER": "x"}) == [("warning", f"{old}WHATEVER is not read: the prefix is MUSIZEER_")]


def test_the_new_names_are_not_reported():
    names = {e.name: "x" for e in environment.ENTRIES}

    assert environment.check(names) == []


def test_the_report_goes_to_the_log_and_only_once_per_process(caplog):
    environment.forget_report()
    try:
        with caplog.at_level(logging.WARNING, logger="core.environment"):
            first = environment.report_at_start({"AUTH_SECRET": CANARY})
            again = environment.report_at_start({"AUTH_SECRET": CANARY})
        assert first == again, "a given environment is always reported"
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [("ERROR", "AUTH_SECRET is no longer read: rename it to MUSIZEER_AUTH_SECRET")] * 2
        assert CANARY not in caplog.text
    finally:
        environment.forget_report()


def test_the_process_environment_is_reported_once(monkeypatch, caplog):
    monkeypatch.setenv("AUTH_SECRET", CANARY)
    monkeypatch.setenv("MUSIZEER_AUTH_SECERT", CANARY)
    environment.forget_report()
    try:
        with caplog.at_level(logging.WARNING, logger="core.environment"):
            environment.report_at_start()
            environment.report_at_start()
        assert sorted(r.getMessage() for r in caplog.records) == [
            "AUTH_SECRET is no longer read: rename it to MUSIZEER_AUTH_SECRET",
            "unknown variable MUSIZEER_AUTH_SECERT; did you mean MUSIZEER_AUTH_SECRET?",
        ]
        assert CANARY not in caplog.text
    finally:
        environment.forget_report()


def test_the_application_reports_at_start(monkeypatch, caplog):
    """``main.lifespan`` reports the old names before anything else."""
    monkeypatch.setenv("CREDENTIALS_KEY", CANARY)
    environment.forget_report()
    try:
        with caplog.at_level(logging.ERROR, logger="core.environment"):
            _run_lifespan(monkeypatch, auth_enabled=False, secret="test")
        assert "CREDENTIALS_KEY is no longer read: rename it to MUSIZEER_CREDENTIALS_KEY" in caplog.text
        assert CANARY not in caplog.text
    finally:
        environment.forget_report()


# ---------------------------------------------------------------------------
# The safety net: the typo guard
# ---------------------------------------------------------------------------


def test_a_mistyped_variable_gets_a_suggestion():
    assert environment.check({"MUSIZEER_AUTH_SECERT": "x"}) == [
        ("warning", "unknown variable MUSIZEER_AUTH_SECERT; did you mean MUSIZEER_AUTH_SECRET?"),
    ]
    assert environment.check({"MUSIZEER_CREDENTIAL_KEY": "x"}) == [
        ("warning", "unknown variable MUSIZEER_CREDENTIAL_KEY; did you mean MUSIZEER_CREDENTIALS_KEY?"),
    ]
    assert environment.check({"MUSIZEER_DB_POOLSIZE": "x"}) == [
        ("warning", "unknown variable MUSIZEER_DB_POOLSIZE; did you mean MUSIZEER_DB_POOL_SIZE?"),
    ]


def test_an_unknown_variable_with_no_likeness_is_still_reported():
    assert environment.check({"MUSIZEER_SOMETHING_ELSE_ENTIRELY": "x"}) == [("warning", "unknown variable MUSIZEER_SOMETHING_ELSE_ENTIRELY")]


def test_variables_that_are_not_the_products_are_left_alone():
    assert environment.check({"PATH": "/bin", "HOME": "/root", "NGINX_ENVSUBST_FILTER": "x", "TZ": "UTC", "VITE_API_URL": "x"}) == []


# ---------------------------------------------------------------------------
# The safety net: a required variable that moved fails the start with the hint
# ---------------------------------------------------------------------------


def test_the_hint_names_the_old_variable_only_when_it_is_the_cause():
    assert environment.moved_hint("MUSIZEER_AUTH_SECRET", {"AUTH_SECRET": CANARY}) == \
        "AUTH_SECRET is no longer read: rename it to MUSIZEER_AUTH_SECRET"
    assert environment.moved_hint("MUSIZEER_AUTH_SECRET", {}) == ""
    assert environment.moved_hint("MUSIZEER_AUTH_SECRET", {"AUTH_SECRET": ""}) == ""
    assert environment.moved_hint("DATABASE_URL", {"DATABASE_URL": "x"}) == "", "a name that did not change has no old one"


def test_the_credentials_key_refusal_names_the_new_variable(monkeypatch):
    monkeypatch.delenv("MUSIZEER_CREDENTIALS_KEY")
    monkeypatch.setenv("CREDENTIALS_KEY", KEY)

    with pytest.raises(credential_cipher.CredentialsKeyError) as refused:
        credential_cipher.require_credentials_key()

    message = str(refused.value)
    assert "CREDENTIALS_KEY is no longer read: rename it to MUSIZEER_CREDENTIALS_KEY" in message
    assert "MUSIZEER_CREDENTIALS_KEY is not set" in message
    assert KEY not in message, "the key is never printed"


def test_the_credentials_key_refusal_has_no_hint_when_the_old_name_is_not_set(monkeypatch):
    monkeypatch.delenv("MUSIZEER_CREDENTIALS_KEY")
    monkeypatch.delenv("CREDENTIALS_KEY", raising=False)

    with pytest.raises(credential_cipher.CredentialsKeyError) as refused:
        credential_cipher.require_credentials_key()

    assert "no longer read" not in str(refused.value)


def test_a_worker_does_not_start_on_the_old_credentials_key(tmp_path):
    """The whole path: a process that has only the old name stops at import, naming the new one, and says so for every
    other old name it finds first."""

    env = {k: v for k, v in os.environ.items() if k not in ("MUSIZEER_CREDENTIALS_KEY", "MUSIZEER_CREDENTIALS_KEY_PREVIOUS", "DATABASE_URL")}
    env.update(CREDENTIALS_KEY=KEY, DATA_DIR="/nonexistent", MUSIZEER_DATA_DIR=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-c", "import workers.celery_app"], cwd=APP_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )

    assert result.returncode != 0
    assert "CREDENTIALS_KEY is no longer read: rename it to MUSIZEER_CREDENTIALS_KEY" in result.stderr
    assert "DATA_DIR is no longer read: rename it to MUSIZEER_DATA_DIR" in result.stderr
    assert KEY not in result.stderr and "/nonexistent" not in result.stderr


def test_the_authentication_secret_refusal_names_the_new_variable(monkeypatch):
    monkeypatch.delenv("MUSIZEER_AUTH_SECRET")
    monkeypatch.setenv("AUTH_SECRET", "k3Qv9Zx1Lm7Tb4Rw8Ny2Hc6Pd5Sj0Fa1")

    with pytest.raises(user_auth.InsecureAuthSecret) as refused:
        user_auth.require_strong_secret()

    message = str(refused.value)
    assert "AUTH_SECRET is no longer read: rename it to MUSIZEER_AUTH_SECRET" in message
    assert "k3Qv9Zx1Lm7Tb4Rw8Ny2Hc6Pd5Sj0Fa1" not in message


def test_the_application_does_not_start_with_authentication_on_and_the_old_secret(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "k3Qv9Zx1Lm7Tb4Rw8Ny2Hc6Pd5Sj0Fa1")

    with pytest.raises(user_auth.InsecureAuthSecret, match="AUTH_SECRET is no longer read: rename it to MUSIZEER_AUTH_SECRET"):
        _run_lifespan(monkeypatch, auth_enabled=True, secret=None)


def test_with_authentication_off_the_old_secret_only_logs(monkeypatch, caplog):
    """Not required while authentication is off: the process starts with the default and says so (decision 2)."""
    monkeypatch.setenv("AUTH_SECRET", "k3Qv9Zx1Lm7Tb4Rw8Ny2Hc6Pd5Sj0Fa1")
    environment.forget_report()
    try:
        with caplog.at_level(logging.ERROR, logger="core.environment"):
            ran = _run_lifespan(monkeypatch, auth_enabled=False, secret=None)
        assert ran == ["system user", "admin bootstrap"]
        assert "AUTH_SECRET is no longer read" in caplog.text
    finally:
        environment.forget_report()


# ---------------------------------------------------------------------------
# One registry per deployable, kept equal
# ---------------------------------------------------------------------------


def test_the_gateway_declares_what_the_application_registry_says_it_reads():
    gateway = gateway_registry()
    declared = {e.name: e for e in environment.ENTRIES if environment.GATEWAY in e.readers}

    assert {e.name for e in gateway.ENTRIES} == set(declared)


def test_the_two_registries_agree_on_every_entry_they_share():
    gateway = gateway_registry()

    for entry in gateway.ENTRIES:
        mine = environment.BY_NAME[entry.name]
        assert (entry.default or "") == (mine.default or ""), f"{entry.name}: the defaults differ"
        assert entry.old_name == mine.old_name, f"{entry.name}: the old names differ"
        assert entry.required == mine.required and entry.secret == mine.secret, f"{entry.name}: the flags differ"
    assert gateway.PREFIX == environment.PREFIX and gateway.OLD_PREFIX == environment.OLD_PREFIX


def test_the_gateway_old_names_are_the_application_ones():
    gateway = gateway_registry()

    for old, new in gateway.REMOVED.items():
        assert environment.BY_NAME[new].old_name == old


# ---------------------------------------------------------------------------
# The folder variables ADR-0008 retired
# ---------------------------------------------------------------------------

FOLDER_VARIABLES = ("WORKSPACE_DIR", "GATEWAY_WORKSPACE_DIR", "GATEWAY_FILESYSTEM_ROOTS")


def test_no_registry_reads_a_folder_from_the_environment():
    gateway = gateway_registry()
    names = [entry.name for entry in (*environment.ENTRIES, *gateway.ENTRIES)]

    assert not [name for name in names if "WORKSPACE" in name or "FILESYSTEM" in name]
    assert not [e.name for e in environment.ENTRIES if e.old_name in FOLDER_VARIABLES]


@pytest.mark.parametrize("variable", FOLDER_VARIABLES)
def test_a_retired_folder_variable_that_is_set_is_an_error_naming_what_replaced_it_under_either_prefix(variable):
    for name in (variable, "MUSIZEER_" + variable):
        findings = environment.check({name: "/secret-looking-path"})

        assert len(findings) == 1 and findings[0][0] == "error", findings
        assert findings[0][1].startswith(f"{name} is no longer read: ") and "Administration" in findings[0][1]
        assert "secret-looking-path" not in findings[0][1], "a value is never printed"


def test_the_working_folder_variable_points_at_the_setting_and_the_roots_at_the_gateways_mounts():
    assert "the working folder is a setting" in environment.RETIRED["WORKSPACE_DIR"]
    assert "root folders are declared" in environment.RETIRED["GATEWAY_FILESYSTEM_ROOTS"]
    assert "mounted" in environment.RETIRED["GATEWAY_FILESYSTEM_ROOTS"]


def test_a_retired_variable_is_not_also_reported_as_a_typo_and_an_empty_one_is_nothing():
    assert len(environment.check({"MUSIZEER_WORKSPACE_DIR": "/x"})) == 1
    assert environment.check({"WORKSPACE_DIR": "", "MUSIZEER_GATEWAY_FILESYSTEM_ROOTS": ""}) == []


def test_the_gateway_retires_the_variables_that_concern_it_with_the_same_words():
    gateway = gateway_registry()

    assert set(gateway.RETIRED) == {name for name in environment.RETIRED if "GATEWAY" in name}
    for name, text in gateway.RETIRED.items():
        assert environment.RETIRED[name] == text


def test_the_old_environment_is_reported_at_start_with_the_new_home_of_each_folder(caplog):
    with caplog.at_level(logging.ERROR, logger="core.environment"):
        environment.report_at_start({"WORKSPACE_DIR": "/a", "GATEWAY_FILESYSTEM_ROOTS": "/b"}, )

    assert "WORKSPACE_DIR is no longer read: the working folder is a setting" in caplog.text
    assert "GATEWAY_FILESYSTEM_ROOTS is no longer read" in caplog.text


@pytest.mark.parametrize("name", ["docker-compose.yaml", "docker-compose.dev.yml"])
def test_compose_passes_no_folder_variable_and_mounts_the_library_in_the_gateway_only(name):
    text = "\n".join(line for line in compose_text(name).splitlines() if not line.lstrip().startswith("#"))

    for variable in FOLDER_VARIABLES:
        assert variable not in text
    services = compose_services(name)
    gateway_volumes = " ".join(map(str, services["provider-gateway"]["volumes"]))
    assert ":/library" in gateway_volumes and ":/downloads" in gateway_volumes
    for service, definition in services.items():
        if service != "provider-gateway":
            assert ":/library" not in " ".join(map(str, definition.get("volumes") or [])), service


# ---------------------------------------------------------------------------
# The compose files
# ---------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}")
# What the development stack decides for itself: the application is reached directly, with no nginx in front, so the
# callback and the landing page are not the public URL's.
DEVELOPMENT_ONLY_DEFAULTS = {
    ("docker-compose.dev.yml", "MUSIZEER_APP_BASE_URL"): "http://localhost:8000",
    ("docker-compose.dev.yml", "MUSIZEER_FRONTEND_URL"): "http://localhost:3000",
}


def compose_text(name: str) -> str:
    return checkout(name).read_text(encoding="utf-8")


def compose_services(name: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(compose_text(name))["services"]


def placeholders(name: str):
    text = "\n".join(line for line in compose_text(name).splitlines() if not line.lstrip().startswith("#"))
    return [(m.group(1), m.group(2), m.group(3)) for m in PLACEHOLDER.finditer(text)]


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_every_variable_a_compose_file_interpolates_is_in_the_registry(name):
    unknown = sorted({var for var, _, _ in placeholders(name) if var not in environment.BY_NAME})

    assert unknown == []


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_compose_repeats_no_default_of_the_registry(name):
    """An optional variable is passed with an EMPTY default (empty means unset, so the registry's default applies); where
    compose itself is the reader (an image tag, a port) its default is the registry's, written once there."""
    wrong = []
    for var, operator, default in placeholders(name):
        entry = environment.BY_NAME[var]
        if operator != ":-" or default == "":
            continue
        if (name, var) in DEVELOPMENT_ONLY_DEFAULTS:
            expected = DEVELOPMENT_ONLY_DEFAULTS[(name, var)]
        elif var == "MUSIZEER_VERSION":
            expected = "latest"  # the tag compose runs; inside an image the version is "dev" (one entry, two roles)
        elif environment.COMPOSE in entry.readers:
            expected = entry.default
        else:
            wrong.append(f"{var}: only the application reads it, compose must pass it empty")
            continue
        if default != expected:
            wrong.append(f"{var}: compose says '{default}', expected '{expected}'")

    assert wrong == []


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_compose_names_the_old_variable_only_in_the_message_of_a_required_one(name):
    old_names = {e.old_name for e in environment.ENTRIES if e.old_name}
    text = "\n".join(line for line in compose_text(name).splitlines() if not line.lstrip().startswith("#"))
    found = []
    for old in sorted(old_names):
        for match in re.finditer(rf"(?<![A-Za-z0-9_]){re.escape(old)}(?![A-Za-z0-9_])", text):
            line = text[text.rfind("\n", 0, match.start()) + 1: text.find("\n", match.end())]
            if ":?" not in line:
                found.append(f"{old}: {line.strip()}")

    assert found == []


@pytest.mark.parametrize("name", ["docker-compose.yaml", "docker-compose.dev.yml"])
def test_every_variable_set_in_a_container_is_one_the_registry_knows(name):
    services = compose_services(name)
    named = {
        key
        for service in services.values()
        for key in (service.get("environment") or {})
    }
    known = {e.name for e in environment.ENTRIES} | {"VITE_API_URL"}
    # The stock postgres image's own variables are the registry's too.

    assert sorted(k for k in named if k.split("=")[0] not in known) == []


def test_the_required_variables_are_required_by_compose_with_the_old_name_in_the_message():
    text = compose_text("docker-compose.yaml")

    assert "${MUSIZEER_AUTH_SECRET:?set MUSIZEER_AUTH_SECRET in .env (it was AUTH_SECRET)}" in text
    assert "${MUSIZEER_CREDENTIALS_KEY:?set MUSIZEER_CREDENTIALS_KEY in .env (it was CREDENTIALS_KEY;" in text
    assert "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}" in text


def test_the_variables_the_code_read_but_compose_never_passed_are_passed_now():
    services = compose_services("docker-compose.yaml")

    def passed(service: str) -> set[str]:
        return set(services[service].get("environment") or {})

    for service in ("init", "app", "worker", "worker-jobs", "worker-maintenance", "beat"):
        assert {
            "MUSIZEER_DB_BUSY_TIMEOUT_MS", "MUSIZEER_DB_STATEMENT_TIMEOUT_MS", "MUSIZEER_DB_POOL_SIZE", "MUSIZEER_DB_MAX_OVERFLOW",
        } <= passed(service), service
    for service in ("app", "worker", "worker-jobs", "worker-maintenance"):
        assert {"MUSIZEER_PUBLIC_API_PREFIX", "MUSIZEER_UPDATE_REPOSITORY"} <= passed(service), service
    assert {"MUSIZEER_ALLOWED_ORIGINS", "MUSIZEER_PUBLIC_URL", "MUSIZEER_APP_BASE_URL", "MUSIZEER_FRONTEND_URL"} <= passed("app")
    assert {"MUSIZEER_GATEWAY_MAX_MEMORY_MB", "MUSIZEER_GATEWAY_MAX_OPEN_FILES", "MUSIZEER_GATEWAY_MAX_PROCESSES"} <= passed("provider-gateway")
    assert {"MUSIZEER_BACKEND_URL", "MUSIZEER_DNS_RESOLVER"} == passed("frontend")


def test_compose_derives_nothing_from_the_public_url():
    """The application derives the callback and the landing page; compose passes the three as they are."""
    app = compose_services("docker-compose.yaml")["app"]["environment"]

    assert app["MUSIZEER_PUBLIC_URL"] == "${MUSIZEER_PUBLIC_URL:-}"
    assert app["MUSIZEER_APP_BASE_URL"] == "${MUSIZEER_APP_BASE_URL:-}"
    assert app["MUSIZEER_FRONTEND_URL"] == "${MUSIZEER_FRONTEND_URL:-}"


def test_the_development_stack_has_two_folders_like_production():
    text = compose_text("docker-compose.dev.yml")

    assert "MUSIC_DIR" not in text
    assert "${MUSIZEER_DOWNLOADS_DIR:-./data/downloads}:/downloads" in text
    assert "${MUSIZEER_LIBRARY_DIR:-./data/library}:/library" in text
    assert "MUSIZEER_DEV_FRONTEND_PORT" in text and "MUSIZEER_DEV_OCI_REGISTRY_PORT" in text


# ---------------------------------------------------------------------------
# .env.example
# ---------------------------------------------------------------------------


def example_names() -> list[str]:
    text = checkout(".env.example").read_text(encoding="utf-8")
    return re.findall(r"^#? ?([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE)


def test_the_example_environment_names_only_variables_of_the_registry():
    assert sorted(set(example_names()) - set(environment.BY_NAME)) == []


def test_the_example_environment_has_no_old_name():
    old = {e.old_name for e in environment.ENTRIES if e.old_name}

    assert sorted(set(example_names()) & old) == []


def test_the_example_environment_lists_every_variable_an_operator_sets():
    """Every registry entry that an operator sets in `.env`. The ones left out are fixed by the images or the volumes compose
    wires, or set by compose from another variable."""
    wired = {
        "MUSIZEER_DATA_DIR",                                                  # the volume compose mounts at /data
        "MUSIZEER_COMMIT",                                                    # baked into the image by the build
        "MUSIZEER_GATEWAY_STATE_DIR", "MUSIZEER_GATEWAY_RUN_DIR",             # the gateway image's folders and volumes
        "MUSIZEER_GATEWAY_SANDBOX_UID", "MUSIZEER_GATEWAY_SANDBOX_GID",
        "MUSIZEER_GATEWAY_SOCKET",                                            # the control socket in the run volume
    }
    missing = [e.name for e in environment.ENTRIES if e.group != environment.TOOLING and e.name not in wired and e.name not in example_names()]

    assert missing == []


def test_the_example_environment_says_the_old_names_are_not_read_and_points_to_the_table():
    text = checkout(".env.example").read_text(encoding="utf-8")

    assert "NOT read any more" in text and "docs/deployment.md" in text


def test_the_example_environment_is_grouped():
    text = checkout(".env.example").read_text(encoding="utf-8")
    positions = [text.index(f"===== {title}") for title in ("Required", "Address", "Database", "Images", "Advanced")]

    assert positions == sorted(positions)


# ---------------------------------------------------------------------------
# The reference table of docs/deployment.md
# ---------------------------------------------------------------------------


def test_the_reference_table_of_the_guide_is_the_registry():
    text = checkout("docs", "deployment.md").read_text(encoding="utf-8")
    start, end = text.index(environment.TABLE_START), text.index(environment.TABLE_END)
    written = text[start + len(environment.TABLE_START): end].strip()

    assert written == environment.reference_table().strip(), (
        "docs/deployment.md differs from the registry: regenerate the block with `python -m core.environment` (from app/) "
        "and paste what it prints between the markers"
    )


def test_the_reference_table_has_every_entry_once_with_its_old_name():
    rows = [line for line in environment.reference_table().splitlines() if line.startswith("| ")]

    for entry in environment.ENTRIES:
        mine = [row for row in rows if row.split(" | ")[1] == f"`{entry.name}`"]
        assert len(mine) == 1, entry.name
        assert mine[0].startswith(f"| `{entry.old_name}` | " if entry.old_name else "| (same) | "), entry.name


def test_the_table_prints_from_the_command():
    result = subprocess.run([sys.executable, "-m", "core.environment"], cwd=APP_ROOT, capture_output=True, text=True, timeout=60)

    assert result.returncode == 0
    assert result.stdout.startswith(environment.TABLE_START) and environment.reference_table().strip() in result.stdout


# ---------------------------------------------------------------------------
# The web app's image
# ---------------------------------------------------------------------------


def frontend_files() -> dict[str, str]:
    return {
        name: checkout("musizeer-ui", *name.split("/")).read_text(encoding="utf-8")
        for name in ("Dockerfile", "nginx/15-backend-url.envsh", "nginx/default.conf.template")
    }


def test_the_web_app_reads_only_its_documented_variables():
    documented = {e.name for e in environment.ENTRIES if environment.FRONTEND in e.readers}
    files = frontend_files()
    named = set(re.findall(r"MUSIZEER_[A-Z_]+", files["nginx/15-backend-url.envsh"] + files["nginx/default.conf.template"]))

    assert documented == {"MUSIZEER_BACKEND_URL", "MUSIZEER_DNS_RESOLVER"}
    assert named == documented


def test_the_image_declares_no_default_for_its_variables():
    dockerfile = frontend_files()["Dockerfile"]

    assert not re.search(r"^\s*ENV\s+(MUSIZEER_)?(BACKEND_URL|DNS_RESOLVER)", dockerfile, flags=re.MULTILINE)
    assert 'NGINX_ENVSUBST_FILTER="^(MUSIZEER_BACKEND_URL|MUSIZEER_DNS_RESOLVER)$"' in dockerfile


def test_the_defaults_of_the_web_app_are_the_registry_ones():
    script = frontend_files()["nginx/15-backend-url.envsh"]

    for name in ("MUSIZEER_BACKEND_URL", "MUSIZEER_DNS_RESOLVER"):
        assert f"{name}=${{{name}:-{environment.BY_NAME[name].default}}}" in script


def test_the_web_app_reports_its_old_names():
    script = frontend_files()["nginx/15-backend-url.envsh"]

    assert "BACKEND_URL:MUSIZEER_BACKEND_URL" in script and "DNS_RESOLVER:MUSIZEER_DNS_RESOLVER" in script
    assert "is no longer read: rename it to" in script


# ---------------------------------------------------------------------------
# The build arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "gateway/Dockerfile", "musizeer-ui/Dockerfile"])
def test_the_images_are_built_with_the_new_names(dockerfile):
    text = checkout(*dockerfile.split("/")).read_text(encoding="utf-8")
    old_product = environment.OLD_PREFIX.rstrip("_")

    assert "ARG MUSIZEER_VERSION=dev" in text
    assert old_product not in text.upper()
    if dockerfile != "musizeer-ui/Dockerfile":
        assert "ARG MUSIZEER_COMMIT=" in text and "ENV MUSIZEER_VERSION=$MUSIZEER_VERSION MUSIZEER_COMMIT=$MUSIZEER_COMMIT" in text
    else:
        assert "ENV VITE_MUSIZEER_VERSION=$MUSIZEER_VERSION" in text


def test_the_workflow_passes_the_new_build_arguments():
    text = checkout(".github", "workflows", "images.yml").read_text(encoding="utf-8")

    assert "MUSIZEER_VERSION=${{ needs.version.outputs.build }}" in text and "MUSIZEER_COMMIT=${{ github.sha }}" in text
