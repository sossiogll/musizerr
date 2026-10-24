"""
The process environment of Musizeer: ONE registry of every variable, and the ONLY module that reads
``os.environ`` (ADR-0009, rule 2 of the boundary; ``tests/test_layering.py`` enforces it).

What a variable is: a name, a default, whether the process refuses to start without it, whether it is
a secret, and the name it had before the rename to Musizeer (``old_name``). The old name is NEVER read
as a value. It exists for the safety net only: at start a process that finds an old name set says so,
with the new name in the message (``report_at_start``), and a required variable that is unset adds the
same hint to the refusal (``moved_hint``). A variable that starts with ``MUSIZEER_`` and is in no entry is
reported with a suggestion (the typo guard). Values are never printed.

The entries also describe what no Python process reads: the variables Docker Compose interpolates
(``COMPOSE``), the ones the web app's image reads in its entrypoint script (``FRONTEND``) and the
gateway's (``GATEWAY``, read by ``gateway/musizeer_gateway/environment.py``, a registry of its own because
the gateway imports only the standard library and the SDK: ``tests/test_environment.py`` keeps the two
equal). They are what ``docs/deployment.md`` prints as the reference table:

    python -m core.environment            # the table, ready to paste between the markers of the guide

Where a default is written here it is the one place: Compose passes an empty value for an optional
variable and the empty value means "unset", so the default below applies.
"""
from __future__ import annotations

import difflib
import logging
import os
from dataclasses import dataclass
from typing import Mapping

logger = logging.getLogger(__name__)

PREFIX = "MUSIZEER_"
# The prefix before the rename (ADR-0010): one letter apart, so a variable spelled with it is a mistake worth saying.
OLD_PREFIX = "MUSIZERR_"

# Who reads a variable.
APP = "app"            # the API, the workers, beat and the scripts
GATEWAY = "gateway"    # the provider gateway
COMPOSE = "compose"    # Docker Compose, when it renders the files; no process of ours sees it
FRONTEND = "frontend"  # the entrypoint script of the web app's image

# How the reference table and ``.env.example`` group them.
REQUIRED = "required"
ADDRESS = "address"
DATABASE = "database"
IMAGES = "images"
ADVANCED = "advanced"
TOOLING = "tooling"
GROUPS = (REQUIRED, ADDRESS, DATABASE, IMAGES, ADVANCED, TOOLING)


@dataclass(frozen=True)
class Variable:
    name: str
    meaning: str
    default: str | None = None
    group: str = ADVANCED
    readers: tuple[str, ...] = (APP,)
    # The process refuses to start without it, always.
    required: bool = False
    # ... or only when this holds (a sentence, for the table).
    required_when: str = ""
    # Never logged, never printed, never put in a message.
    secret: bool = False
    # What it was called before ADR-0009 / ADR-0010. Safety net only: never read as a value.
    old_name: str | None = None
    # How the table shows a default that is not a literal (a derived one, or one that differs by role).
    default_doc: str = ""


ENTRIES: tuple[Variable, ...] = (
    # ---- required ---------------------------------------------------------------------------------------------
    Variable(
        "MUSIZEER_CREDENTIALS_KEY",
        "The Fernet key that seals every stored credential (docs/credentials.md). Back it up with the database.",
        group=REQUIRED, required=True, secret=True, old_name="CREDENTIALS_KEY",
    ),
    Variable(
        "MUSIZEER_AUTH_SECRET",
        "Signs the session tokens: at least 32 random characters, not a placeholder (docs/permissions.md).",
        group=REQUIRED, required_when="authentication is on", secret=True, old_name="AUTH_SECRET",
    ),
    Variable(
        "POSTGRES_PASSWORD",
        "The password of the `postgres` service and of the database URL compose builds; letters and digits, as it goes into a URL.",
        group=REQUIRED, readers=(COMPOSE,), required=True, secret=True,
    ),
    # ---- address ----------------------------------------------------------------------------------------------
    Variable(
        "MUSIZEER_PUBLIC_URL",
        "The address users type in the browser, no trailing slash; the OAuth2 redirect URI and the end of a sign-in derive from it.",
        default="http://localhost", group=ADDRESS, readers=(APP, COMPOSE), old_name="PUBLIC_URL",
    ),
    Variable(
        "MUSIZEER_PORT",
        "The port the web app is published on, on the host.",
        default="80", group=ADDRESS, readers=(COMPOSE,), old_name="APP_PORT",
    ),
    Variable(
        "MUSIZEER_APP_BASE_URL",
        "The base of the OAuth2 callback; set it only when it is not `MUSIZEER_PUBLIC_URL` + `/api`.",
        group=ADDRESS, old_name="APP_BASE_URL", default_doc="`MUSIZEER_PUBLIC_URL` + `/api`",
    ),
    Variable(
        "MUSIZEER_FRONTEND_URL",
        "Where the browser is sent when a sign-in ends; set it only when it is not `MUSIZEER_PUBLIC_URL`.",
        group=ADDRESS, old_name="FRONTEND_URL", default_doc="`MUSIZEER_PUBLIC_URL`",
    ),
    Variable(
        "MUSIZEER_BACKEND_URL",
        "Where the web app's nginx reaches the API: scheme://host:port, no path.",
        default="http://app:8000", group=ADDRESS, readers=(FRONTEND, COMPOSE), old_name="BACKEND_URL",
    ),
    Variable(
        "MUSIZEER_DNS_RESOLVER",
        "The name server the web app's nginx asks for the API's address.",
        default="127.0.0.11", group=ADDRESS, readers=(FRONTEND,), old_name="DNS_RESOLVER",
    ),
    Variable(
        "MUSIZEER_PUBLIC_API_PREFIX",
        "The prefix the browser reaches the API with, applied to the cover, preview and stream URLs the API answers.",
        default="/api", group=ADDRESS, old_name="PUBLIC_API_PREFIX",
    ),
    Variable(
        "MUSIZEER_ALLOWED_ORIGINS",
        "The origins CORS allows, separated by commas.",
        default="*", group=ADDRESS, old_name="ALLOWED_ORIGINS",
    ),
    # ---- database ---------------------------------------------------------------------------------------------
    Variable(
        "DATABASE_URL",
        "Where the data lives (`postgresql+psycopg://user:password@host:5432/name`). Compose builds it from `POSTGRES_*`.",
        group=DATABASE, secret=True, default_doc="unset: the SQLite file in `MUSIZEER_DATA_DIR`",
    ),
    Variable(
        "MUSIZEER_DATA_DIR",
        "The folder of the SQLite file (when there is no `DATABASE_URL`), the migration backups and the legacy `config.json`.",
        default="/data", group=DATABASE, old_name="DATA_DIR",
    ),
    Variable(
        "REDIS_URL",
        "The Celery broker and the progress channel.",
        default="redis://redis:6379/0", group=DATABASE,
    ),
    Variable(
        "POSTGRES_USER",
        "The user of the `postgres` service.",
        default="musizeer", group=DATABASE, readers=(COMPOSE,),
    ),
    Variable(
        "POSTGRES_DB",
        "The database of the `postgres` service.",
        default="musizeer", group=DATABASE, readers=(COMPOSE,),
    ),
    Variable(
        "MUSIZEER_DB_BUSY_TIMEOUT_MS",
        "How long (ms) a write waits for another writer's lock before it answers 503 `database_busy`.",
        default="15000", group=DATABASE, old_name="DB_BUSY_TIMEOUT_MS",
    ),
    Variable(
        "MUSIZEER_DB_STATEMENT_TIMEOUT_MS",
        "PostgreSQL: a query that runs longer (ms) is interrupted; 0 never.",
        default="120000", group=DATABASE, old_name="DB_STATEMENT_TIMEOUT_MS",
    ),
    Variable(
        "MUSIZEER_DB_POOL_SIZE",
        "PostgreSQL: connections kept per process (the API and each worker).",
        default="5", group=DATABASE, old_name="DB_POOL_SIZE",
    ),
    Variable(
        "MUSIZEER_DB_MAX_OVERFLOW",
        "PostgreSQL: connections a process may open beyond the pool.",
        default="10", group=DATABASE, old_name="DB_MAX_OVERFLOW",
    ),
    Variable(
        "MUSIZEER_MIGRATION_BACKUP",
        "`off` skips the backup `init` takes before it migrates the database (docs/migrations.md).",
        default="on", group=DATABASE, old_name="MIGRATION_BACKUP",
    ),
    Variable(
        "MUSIZEER_MIGRATION_BACKUPS_KEEP",
        "How many migration backups are kept in `MUSIZEER_DATA_DIR`/backups.",
        default="5", group=DATABASE, old_name="MIGRATION_BACKUPS_KEEP",
    ),
    # ---- images -----------------------------------------------------------------------------------------------
    Variable(
        "MUSIZEER_VERSION",
        "In `.env`: the tag of the images compose runs. Inside an image: the version it was built as (a build argument).",
        default="dev", group=IMAGES, readers=(APP, GATEWAY, COMPOSE), old_name="MUSIZERR_VERSION",
        default_doc="`latest` (the compose tag); `dev` inside a local build",
    ),
    Variable(
        "MUSIZEER_COMMIT",
        "The commit an image was built from (a build argument; compose does not forward it).",
        default="", group=IMAGES, readers=(APP, GATEWAY), old_name="MUSIZERR_COMMIT",
    ),
    Variable(
        "MUSIZEER_IMAGES",
        "The registry and owner of the images (a fork).",
        default="ghcr.io/sossiogll", group=IMAGES, readers=(COMPOSE,), old_name="MUSIZERR_IMAGES",
    ),
    Variable(
        "MUSIZEER_UPDATE_REPOSITORY",
        "The GitHub repository whose releases are this instance's updates (`owner/name`).",
        default="sossiogll/musizeer", group=IMAGES, old_name="MUSIZERR_UPDATE_REPOSITORY",
    ),
    # ---- advanced ---------------------------------------------------------------------------------------------
    Variable(
        "MUSIZEER_CREDENTIALS_KEY_PREVIOUS",
        "The keys `MUSIZEER_CREDENTIALS_KEY` replaced, separated by commas, while a rotation is in progress.",
        secret=True, old_name="CREDENTIALS_KEY_PREVIOUS",
    ),
    Variable(
        "MUSIZEER_ADMIN_BOOTSTRAP_EMAIL",
        "With the password below, creates the first administrator when there are no users.",
        old_name="ADMIN_BOOTSTRAP_EMAIL",
    ),
    Variable(
        "MUSIZEER_ADMIN_BOOTSTRAP_PASSWORD",
        "The first administrator's password (at least 8 characters).",
        secret=True, old_name="ADMIN_BOOTSTRAP_PASSWORD",
    ),
    Variable(
        "MUSIZEER_GATEWAY_SOCKET",
        "The control socket of the provider gateway, as the application sees it.",
        default="/run/musizeer/gateway.sock", old_name="PROVIDER_GATEWAY_SOCKET",
    ),
    # Removed by ADR-0008: the working folder becomes a folder setting over a root folder, and the host pushes the
    # roots and the working folder to the gateway. The three entries below go with the pull request that does it.
    Variable(
        "MUSIZEER_WORKSPACE_DIR",
        "The folder the host and the gateway exchange files through (the default of the `workspace_dir` setting).",
        default="/downloads/.musizeer-work", old_name="WORKSPACE_DIR",
    ),
    Variable(
        "MUSIZEER_GATEWAY_WORKSPACE_DIR",
        "The same folder as the gateway sees it; compose sets both from `MUSIZEER_WORKSPACE_DIR`.",
        default="/downloads/.musizeer-work", readers=(GATEWAY,), old_name="GATEWAY_WORKSPACE_DIR",
    ),
    Variable(
        "MUSIZEER_GATEWAY_FILESYSTEM_ROOTS",
        "The folders a provider may be given a path inside of, separated by `:`.",
        default="", readers=(GATEWAY,), old_name="GATEWAY_FILESYSTEM_ROOTS",
        default_doc="empty (compose passes `/downloads`)",
    ),
    Variable(
        "MUSIZEER_GATEWAY_STATE_DIR",
        "Everything the gateway keeps: layer cache, unpacked images, egress sockets.",
        default="/var/lib/musizeer-gateway", readers=(GATEWAY,), old_name="GATEWAY_STATE_DIR",
    ),
    Variable(
        "MUSIZEER_GATEWAY_RUN_DIR",
        "Shared with the application: the control socket and the sockets of the providers.",
        default="/run/musizeer", readers=(GATEWAY,), old_name="GATEWAY_RUN_DIR",
    ),
    Variable(
        "MUSIZEER_GATEWAY_SANDBOX_UID",
        "The uid a provider sees itself as inside its sandbox.",
        default="10001", readers=(GATEWAY,), old_name="GATEWAY_SANDBOX_UID",
    ),
    Variable(
        "MUSIZEER_GATEWAY_SANDBOX_GID",
        "The gid a provider sees itself as inside its sandbox.",
        default="10001", readers=(GATEWAY,), old_name="GATEWAY_SANDBOX_GID",
    ),
    Variable(
        "MUSIZEER_GATEWAY_MAX_MEMORY_MB",
        "The most memory (MB) any provider may be given, whatever its manifest asks.",
        default="2048", readers=(GATEWAY,), old_name="GATEWAY_MAX_MEMORY_MB",
    ),
    Variable(
        "MUSIZEER_GATEWAY_MAX_OPEN_FILES",
        "The most open files any provider may be given.",
        default="4096", readers=(GATEWAY,), old_name="GATEWAY_MAX_OPEN_FILES",
    ),
    Variable(
        "MUSIZEER_GATEWAY_MAX_PROCESSES",
        "The most processes and threads any one provider sandbox may hold.",
        default="1024", readers=(GATEWAY,), old_name="GATEWAY_MAX_PROCESSES",
    ),
    Variable(
        "MUSIZEER_GATEWAY_PIDS_LIMIT",
        "The pids limit of the gateway container: the gateway and every sandbox together.",
        default="4096", readers=(COMPOSE,), old_name="GATEWAY_PIDS_LIMIT",
    ),
    Variable(
        "MUSIZEER_GATEWAY_SECCOMP_PROFILE",
        "The seccomp profile of the gateway container, a file on the machine that runs Docker.",
        default="./gateway/seccomp/musizeer-gateway.json", readers=(COMPOSE,), old_name="GATEWAY_SECCOMP_PROFILE",
    ),
    # ---- the development stack (docker-compose.dev.yml) --------------------------------------------------------
    Variable(
        "MUSIZEER_DOWNLOADS_DIR",
        "Development stack: the staging folder, mounted as `/downloads` in the application, the workers and the gateway.",
        default="./data/downloads", readers=(COMPOSE,), old_name="MUSIC_DIR",
    ),
    Variable(
        "MUSIZEER_LIBRARY_DIR",
        "Development stack: the library folder, mounted as `/library` in the gateway.",
        default="./data/library", readers=(COMPOSE,), old_name="MUSIC_DIR",
    ),
    Variable(
        "MUSIZEER_DEV_FRONTEND_PORT",
        "Development stack: the port of the Vite dev server on the host.",
        default="3000", readers=(COMPOSE,), old_name="FRONTEND_PORT",
    ),
    Variable(
        "MUSIZEER_DEV_OCI_REGISTRY_PORT",
        "Development stack: the loopback port of the local OCI registry.",
        default="15000", readers=(COMPOSE,), old_name="OCI_REGISTRY_PORT",
    ),
    # ---- tooling (the end-to-end tests) ------------------------------------------------------------------------
    Variable("E2E_USERNAME", "End-to-end tests: the user `scripts.e2e_user` creates.", default="e2e", group=TOOLING),
    Variable("E2E_PASSWORD", "End-to-end tests: that user's password.", group=TOOLING, secret=True),
    Variable("E2E_ADMIN_USERNAME", "End-to-end tests: the administrator `scripts.e2e_user` creates.", default="e2e-admin", group=TOOLING),
    Variable("E2E_ADMIN_PASSWORD", "End-to-end tests: that administrator's password.", group=TOOLING, secret=True),
)

BY_NAME: dict[str, Variable] = {entry.name: entry for entry in ENTRIES}

# Old name -> new name, for the variables a process of the application used to read: the safety net's table.
REMOVED: dict[str, str] = {
    entry.old_name: entry.name for entry in ENTRIES if entry.old_name and APP in entry.readers
}


class NotRead(KeyError):
    """The application does not read this variable (it is not in the registry, or another deployable reads it)."""


def _entry(name: str) -> Variable:
    entry = BY_NAME.get(name)
    if entry is None or APP not in entry.readers:
        raise NotRead(name)
    return entry


def default(name: str) -> str:
    """The default of ``name`` as text; "" when it has none."""
    return _entry(name).default or ""


def get(name: str, environ: Mapping[str, str] | None = None) -> str:
    """The value of ``name``, or its default. An empty value is an unset one (compose passes an empty value for an
    optional variable, so the default applies); the text is otherwise returned as it is."""
    entry = _entry(name)
    value = (os.environ if environ is None else environ).get(name, "")
    return value if value != "" else (entry.default or "")


def get_int(name: str, environ: Mapping[str, str] | None = None) -> int:
    """``name`` as a whole number; the default when the value is not one."""
    try:
        return int(get(name, environ))
    except ValueError:
        return int(default(name))


def redis_url() -> str:
    return get("REDIS_URL")


def data_dir() -> str:
    return get("MUSIZEER_DATA_DIR")


def public_url() -> str:
    return get("MUSIZEER_PUBLIC_URL").rstrip("/")


def app_base_url() -> str:
    """The base of the OAuth2 callback (``<base>/auth/<provider>/callback``): the variable, else the public URL + ``/api``."""
    return (get("MUSIZEER_APP_BASE_URL") or public_url() + "/api").rstrip("/")


def frontend_url() -> str:
    """Where the browser lands when a sign-in ends: the variable, else the public URL."""
    return (get("MUSIZEER_FRONTEND_URL") or public_url()).rstrip("/")


def snapshot() -> dict[str, str]:
    """A copy of the whole environment: for the two places that need ALL of it, the environment a child process
    inherits (``db/migration_backup.py``) and a variable the user names on a command line (``--token-env``)."""
    return dict(os.environ)


# ---------------------------------------------------------------------------------------------------------------
# The safety net: old names, a required variable that moved, a typo.
# ---------------------------------------------------------------------------------------------------------------

def moved_message(old: str, new: str) -> str:
    return f"{old} is no longer read: rename it to {new}"


def moved_hint(name: str, environ: Mapping[str, str] | None = None) -> str:
    """``"AUTH_SECRET is no longer read: rename it to MUSIZEER_AUTH_SECRET"`` when ``name`` is unset but its old name is
    set; "" otherwise. For the refusal of a required variable (the message of the failed start names the new one)."""
    entry = BY_NAME.get(name)
    source = os.environ if environ is None else environ
    if entry is None or not entry.old_name or source.get(entry.old_name, "") == "":
        return ""
    return moved_message(entry.old_name, entry.name)


def check(environ: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """The findings of the safety net over ``environ`` as ``(level, message)``, ``level`` being "error" or "warning".
    A message names variables, never values."""
    source = os.environ if environ is None else environ
    findings: list[tuple[str, str]] = []
    for old, new in REMOVED.items():
        if source.get(old, "") != "":
            findings.append(("error", moved_message(old, new)))
    known = list(BY_NAME)
    for name in sorted(source):
        if name.startswith(PREFIX) and name not in BY_NAME:
            near = difflib.get_close_matches(name, known, n=1, cutoff=0.75)
            hint = f"; did you mean {near[0]}?" if near else ""
            findings.append(("warning", f"unknown variable {name}{hint}"))
        elif name.startswith(OLD_PREFIX) and name not in REMOVED:
            renamed = PREFIX + name[len(OLD_PREFIX):]
            near = [renamed] if renamed in BY_NAME else difflib.get_close_matches(renamed, known, n=1, cutoff=0.75)
            hint = f"; did you mean {near[0]}?" if near else f": the prefix is {PREFIX}"
            findings.append(("warning", f"{name} is not read{hint}"))
    return findings


_reported = False


def report_at_start(environ: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """Log the findings of ``check``: an old name that is set is an ERROR (the process starts with the default, or refuses
    to if the new variable is required), an unknown ``MUSIZEER_`` variable a warning. Every entry point calls it; the
    process environment is reported once per process (the API imports the workers' module too), a given ``environ``
    always."""
    global _reported
    if environ is None:
        if _reported:
            return []
        _reported = True
    findings = check(environ)
    for level, message in findings:
        (logger.error if level == "error" else logger.warning)("%s", message)
    return findings


def forget_report() -> None:
    """Report the process environment again at the next ``report_at_start`` (tests)."""
    global _reported
    _reported = False


# ---------------------------------------------------------------------------------------------------------------
# The reference table of docs/deployment.md.
# ---------------------------------------------------------------------------------------------------------------

TABLE_START = "<!-- environment-reference:start -->"
TABLE_END = "<!-- environment-reference:end -->"
_GROUP_TITLES = {
    REQUIRED: "Required", ADDRESS: "Address", DATABASE: "Database and data", IMAGES: "Images and versions",
    ADVANCED: "Advanced", TOOLING: "Tests and tooling",
}
_READERS = {APP: "application", GATEWAY: "gateway", COMPOSE: "compose", FRONTEND: "web app image"}


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def reference_table(entries=ENTRIES) -> str:
    """The Markdown of the reference table: one section per group, old name -> new name -> meaning -> default."""
    lines: list[str] = []
    for group in GROUPS:
        members = [entry for entry in entries if entry.group == group]
        if not members:
            continue
        lines += [f"#### {_GROUP_TITLES[group]}", "", "| Old name | New name | Meaning | Default | Required | Used by |", "|---|---|---|---|---|---|"]
        for entry in members:
            old = f"`{entry.old_name}`" if entry.old_name else "(same)"
            if entry.default_doc:
                default_text = entry.default_doc
            else:
                default_text = f"`{entry.default}`" if entry.default else "none"
            required = "yes" if entry.required else (f"when {entry.required_when}" if entry.required_when else "no")
            meaning = entry.meaning + (" Secret." if entry.secret else "")
            used = ", ".join(_READERS[reader] for reader in entry.readers)
            lines.append(f"| {old} | `{entry.name}` | {_cell(meaning)} | {_cell(default_text)} | {required} | {used} |")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


if __name__ == "__main__":
    print(TABLE_START)
    print()
    print(reference_table())
    print(TABLE_END)
