"""
The environment of the provider gateway: ONE registry of the variables it reads, and the ONLY module of the gateway
that touches ``os.environ`` (ADR-0009; ``tests/test_layering.py`` enforces it).

The application has a registry of its own (``app/core/environment.py``) and so has the gateway: the gateway imports
only the standard library and the SDK (ADR-0006), and the SDK is the providers' contract, which is no place for the
names of a deployment. The two are kept equal by a test (``app/tests/test_environment.py``): every entry here is also
there, with the same default, the same old name and the same flags.

An entry has a name, a default, whether it is required or secret, and its OLD name. The old name is never read as a
value: at start a gateway that finds one set logs an error naming the new one (``report_at_start``). An empty value
is an unset one, so Docker Compose can pass a variable it was not given (``${MUSIZEER_GATEWAY_MAX_MEMORY_MB:-}``) and
the default below applies.
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


@dataclass(frozen=True)
class Variable:
    name: str
    default: str | None = None
    required: bool = False
    secret: bool = False
    # What it was called before ADR-0009 / ADR-0010. Safety net only: never read as a value.
    old_name: str | None = None


ENTRIES: tuple[Variable, ...] = (
    Variable("MUSIZEER_VERSION", default="dev", old_name="MUSIZERR_VERSION"),
    Variable("MUSIZEER_COMMIT", default="", old_name="MUSIZERR_COMMIT"),
    Variable("MUSIZEER_GATEWAY_STATE_DIR", default="/var/lib/musizeer-gateway", old_name="GATEWAY_STATE_DIR"),
    Variable("MUSIZEER_GATEWAY_RUN_DIR", default="/run/musizeer", old_name="GATEWAY_RUN_DIR"),
    # Removed by ADR-0008: the host pushes the working folder and the declared root folders to the gateway.
    Variable("MUSIZEER_GATEWAY_WORKSPACE_DIR", default="/downloads/.musizeer-work", old_name="GATEWAY_WORKSPACE_DIR"),
    Variable("MUSIZEER_GATEWAY_FILESYSTEM_ROOTS", default="", old_name="GATEWAY_FILESYSTEM_ROOTS"),
    Variable("MUSIZEER_GATEWAY_SANDBOX_UID", default="10001", old_name="GATEWAY_SANDBOX_UID"),
    Variable("MUSIZEER_GATEWAY_SANDBOX_GID", default="10001", old_name="GATEWAY_SANDBOX_GID"),
    Variable("MUSIZEER_GATEWAY_MAX_MEMORY_MB", default="2048", old_name="GATEWAY_MAX_MEMORY_MB"),
    Variable("MUSIZEER_GATEWAY_MAX_OPEN_FILES", default="4096", old_name="GATEWAY_MAX_OPEN_FILES"),
    Variable("MUSIZEER_GATEWAY_MAX_PROCESSES", default="1024", old_name="GATEWAY_MAX_PROCESSES"),
)

BY_NAME: dict[str, Variable] = {entry.name: entry for entry in ENTRIES}

# Old name -> new name: the safety net's table.
REMOVED: dict[str, str] = {entry.old_name: entry.name for entry in ENTRIES if entry.old_name}


def get(name: str, environ: Mapping[str, str] | None = None) -> str:
    """The value of ``name``, or its default. An empty value is an unset one."""
    entry = BY_NAME[name]
    value = (os.environ if environ is None else environ).get(name, "")
    return value if value != "" else (entry.default or "")


def get_int(name: str, environ: Mapping[str, str] | None = None) -> int:
    """``name`` as a whole number; a value that is not one is an error that names the variable."""
    value = get(name, environ)
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, not '{value}'") from None


def moved_message(old: str, new: str) -> str:
    return f"{old} is no longer read: rename it to {new}"


def check(environ: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """The findings of the safety net over ``environ`` as ``(level, message)``. A message names variables, never values.
    An unknown ``MUSIZEER_`` variable is reported only when it is close to one of the gateway's own: the others belong
    to the application, which has its own registry."""
    source = os.environ if environ is None else environ
    findings: list[tuple[str, str]] = []
    for old, new in REMOVED.items():
        if source.get(old, "") != "":
            findings.append(("error", moved_message(old, new)))
    known = list(BY_NAME)
    for name in sorted(source):
        if name.startswith(PREFIX) and name not in BY_NAME:
            near = difflib.get_close_matches(name, known, n=1, cutoff=0.85)
            if near:
                findings.append(("warning", f"unknown variable {name}; did you mean {near[0]}?"))
        elif name.startswith(OLD_PREFIX) and name not in REMOVED:
            renamed = PREFIX + name[len(OLD_PREFIX):]
            if renamed in BY_NAME:
                findings.append(("warning", f"{name} is not read; did you mean {renamed}?"))
    return findings


def report_at_start(environ: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """Log the findings of ``check``: an old name that is set is an ERROR, a likely typo a warning."""
    findings = check(environ)
    for level, message in findings:
        (logger.error if level == "error" else logger.warning)("%s", message)
    return findings
