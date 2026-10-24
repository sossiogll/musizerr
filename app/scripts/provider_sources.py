"""
Sources and installed providers from the host's shell, for the server admin (no web login): the
same services as the ``/sources`` routes (``services/sources.py``), so what is installed here is
what the web page shows and the gateway runs.

Uso, dalla cartella ``app/`` (``docker compose exec app python -m scripts.provider_sources ...``)::

    python -m scripts.provider_sources list
    python -m scripts.provider_sources add-source <url> [--insecure] [--credential-env VAR] [--dry-run]
    python -m scripts.provider_sources edit-source <source> [--insecure | --secure]
                                       [--credential-env VAR | --clear-credential] [--enable | --disable]
    python -m scripts.provider_sources catalog <source>
    python -m scripts.provider_sources install <source> <name> [--yes] [--dry-run]
    python -m scripts.provider_sources update <name> [--yes] [--dry-run]
    python -m scripts.provider_sources uninstall <name> [--dry-run]

``<source>`` is the id, the exact URL or the name of a source. ``install`` and ``update`` print what
the admin is approving (the image by digest, the permissions; for an update, what changed) and ask
before writing, unless ``--yes``; without a terminal and without ``--yes`` they do not write.

A token is read from the environment variable named by ``--credential-env`` — NEVER from the
command line, where it would stay in the shell history and the process list — and is never
printed. Exit status: 0 done, 1 refused (the code and the reason are printed), 2 not done
(usage, or the answer was no).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Mapping, TextIO

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session  # noqa: E402

from db import credential_cipher  # noqa: E402
from services.sources import SourceError, SourceManager  # noqa: E402


class _NoTokenOnTheCommandLine(argparse.Action):
    """``--credential`` and ``--token`` exist only to refuse: the message does not repeat what was typed."""

    def __call__(self, parser, namespace, values, option_string=None):
        parser.error(
            f"{option_string} is not accepted: a token on the command line stays in the shell history and the process list. "
            "Put it in an environment variable and name that variable with --credential-env."
        )


def _refuse_tokens(parser: argparse.ArgumentParser) -> None:
    for option in ("--credential", "--token"):
        parser.add_argument(option, action=_NoTokenOnTheCommandLine, nargs="?", help=argparse.SUPPRESS)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        # What was typed is not repeated: it might be a token (`--cred <token>`).
        if message.startswith("unrecognized arguments"):
            message = "unrecognized arguments (not repeated here: one of them may be a token)"
        super().error(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m scripts.provider_sources", description=__doc__.split("\n\n")[0], allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    def command(name: str, help_: str) -> argparse.ArgumentParser:
        return commands.add_parser(name, help=help_, allow_abbrev=False)

    command("list", "the sources and the installed providers (no network)")

    add = command("add-source", "add a source: fetch and validate its catalog, then store it")
    add.add_argument("url")
    add.add_argument("--insecure", action="store_true", help="allow plain HTTP to the image registry of this source (a local registry of images under test)")
    add.add_argument("--credential-env", metavar="VAR", help="the environment variable that holds the token of a private catalog and its images")
    add.add_argument("--dry-run", action="store_true", help="fetch and validate, store nothing")
    _refuse_tokens(add)

    edit = command("edit-source", "change a source: its registry mode, its token, whether it is enabled")
    edit.add_argument("source")
    mode = edit.add_mutually_exclusive_group()
    mode.add_argument("--insecure", dest="insecure", action="store_const", const=True, default=None)
    mode.add_argument("--secure", dest="insecure", action="store_const", const=False)
    token = edit.add_mutually_exclusive_group()
    token.add_argument("--credential-env", metavar="VAR")
    token.add_argument("--clear-credential", action="store_true")
    _refuse_tokens(edit)
    state = edit.add_mutually_exclusive_group()
    state.add_argument("--enable", dest="enabled", action="store_const", const=True, default=None)
    state.add_argument("--disable", dest="enabled", action="store_const", const=False)

    catalog = command("catalog", "fetch and show the catalog of a source")
    catalog.add_argument("source")

    install = command("install", "install a provider from a source, approving what is shown")
    install.add_argument("source")
    install.add_argument("name")
    install.add_argument("--yes", action="store_true", help="do not ask")
    install.add_argument("--dry-run", action="store_true", help="show what would be approved, install nothing")

    update = command("update", "approve and apply the update a provider's catalog offers")
    update.add_argument("name")
    update.add_argument("--yes", action="store_true", help="do not ask")
    update.add_argument("--dry-run", action="store_true", help="show the difference, change nothing")

    uninstall = command("uninstall", "uninstall a provider")
    uninstall.add_argument("name")
    uninstall.add_argument("--dry-run", action="store_true", help="say what would be uninstalled, do nothing")
    return parser


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------


def describe_permissions(permissions: Mapping) -> list[str]:
    """What a provider's sandbox is granted, in words (the JSON form of ``core.catalog``)."""
    lines = [_network(host) for host in permissions.get("egress") or ()]
    lines += [f"network: whatever the setting '{key}' names (a URL or host the user types)" for key in permissions.get("egress_from_settings") or ()]
    lines += [_tcp(item) for item in permissions.get("tcp_from_settings") or ()]
    lines += [f"files: the folder the setting '{item['setting']}' names ({'read and write' if item['mode'] == 'rw' else 'read only'})" for item in permissions.get("filesystem_from_settings") or ()]
    limits = {name: value for name, value in (permissions.get("limits") or {}).items() if value is not None}
    if limits:
        lines.append("limits: " + ", ".join(f"{name.replace('_', ' ')} {value}" for name, value in sorted(limits.items())))
    return lines or ["no network, no files, the gateway's default limits"]


def describe_diff(diff: Mapping) -> list[str]:
    lines = []
    for sign, key in (("+", "added"), ("-", "removed")):
        lines += [f"{sign} {_network(host)}" for host in diff["egress"][key]]
        lines += [f"{sign} {_tcp(item)}" for item in diff["tcp_from_settings"][key]]
        lines += [f"{sign} network from the setting '{name}'" for name in diff["egress_from_settings"][key]]
        lines += [f"{sign} files from the setting '{item['setting']}' ({item['mode']})" for item in diff["filesystem_from_settings"][key]]
        lines += [f"{sign} role {role}" for role in diff["roles"][key]]
    lines += [
        f"~ limit {change['name'].replace('_', ' ')}: {change['old'] if change['old'] is not None else 'default'} -> {change['new'] if change['new'] is not None else 'default'}"
        for change in diff["limits"]
    ]
    return lines


def _network(host: str) -> str:
    if host.startswith("*."):
        return f"network: {host} (any subdomain, not {host[2:].rpartition(':')[0]} itself)"
    return f"network: {host}"


def _tcp(item: Mapping) -> str:
    ports = "any port" if item["ports"] == "any" else f"port {item['ports']}"
    return f"network: {ports} of the host the setting '{item['setting']}' names (a protocol that is not HTTP, such as FTP)"


# ------------------------------------------------------------------
# The commands
# ------------------------------------------------------------------


class Cli:
    def __init__(self, manager: SourceManager, out: TextIO, environ: Mapping[str, str], stdin: TextIO) -> None:
        self.manager = manager
        self.out = out
        self.environ = environ
        self.stdin = stdin

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def token_from(self, variable: str | None) -> str | None:
        if variable is None:
            return None
        value = self.environ.get(variable, "").strip()
        if not value:
            raise SourceError("invalid_credential", f"the environment variable {variable} is not set or is empty", 422)
        return value

    def confirm(self, question: str, assume_yes: bool) -> bool:
        if assume_yes:
            return True
        if not self.stdin.isatty():
            self.say("Nothing was written: there is no terminal to ask on (use --yes to approve).")
            return False
        self.say(f"{question} [y/N] ")
        answer = self.stdin.readline().strip().lower()
        if answer not in ("y", "yes"):
            self.say("Nothing was written.")
            return False
        return True

    # ---- commands --------------------------------------------------

    def list(self, _args) -> int:
        sources = self.manager.list_sources()
        self.say("Sources:" if sources else "No sources.")
        for source in sources:
            flags = ", ".join(flag for flag, on in (("disabled", not source.enabled), ("token set", bool(source.credential)), ("insecure registry", source.allow_insecure_registry)) if on)
            self.say(f"  {source.name}  {source.url}  [{source.id}]" + (f"  ({flags})" if flags else ""))
        installed = self.manager.list_installed(check_updates=False)
        self.say("Installed providers:" if installed else "No providers installed.")
        for item in installed:
            self.say(f"  {item['name']}  {item['image']}@{item['digest']}  from {item['source_name'] or '?'}  roles: {', '.join(item['roles'])}")
        return 0

    def add_source(self, args) -> int:
        credential = self.token_from(args.credential_env)
        if args.dry_run:
            catalog = self.manager.preview_source(args.url, credential, args.insecure)
            self.say(f"Dry run: '{catalog.name}' is a usable catalog with {len(catalog.entries)} providers"
                     + (f" and {len(catalog.rejected)} entries that are not valid." if catalog.rejected else "."))
            return 0
        source = self.manager.add_source(args.url, credential, args.insecure)
        self.say(f"Added '{source.name}' ({source.id})" + (" with a token." if source.credential else "."))
        return 0

    def edit_source(self, args) -> int:
        from services.sources import UNSET

        source = self.manager.resolve_source(args.source)
        credential = None if args.clear_credential else self.token_from(args.credential_env) if args.credential_env else UNSET
        self.manager.edit_source(
            source.id,
            enabled=UNSET if args.enabled is None else args.enabled,
            credential=credential,
            allow_insecure_registry=UNSET if args.insecure is None else args.insecure,
        )
        self.say(f"Updated '{source.name}'.")
        return 0

    def catalog(self, args) -> int:
        source = self.manager.resolve_source(args.source)
        page = self.manager.browse(source.id)
        self.say(f"{page['name']}  ({source.url})")
        for item in page["entries"]:
            state = "installed" if item["installed"] else "not installed"
            if item["update"]:
                state += ", update available"
            if not item["compatible"]:
                state = f"cannot be installed here: {item['incompatibility']}"
            self.say(f"  {item['name']}  {item['label']}  {item['image']}@{item['digest']}  roles: {', '.join(item['roles'])}  [{state}]")
        for rejected in page["rejected"]:
            self.say(f"  ! {rejected['name']}: not valid: {rejected['reason']}")
        return 0

    def install(self, args) -> int:
        source = self.manager.resolve_source(args.source)
        page = self.manager.browse(source.id)
        item = next((entry for entry in page["entries"] if entry["name"] == args.name), None)
        if item is None:
            raise SourceError("provider_not_in_catalog", f"'{args.name}' is not in the catalog of '{source.name}'", 404)
        if not item["compatible"]:
            raise SourceError("incompatible_provider", f"'{args.name}' {item['incompatibility']}", 422)
        self.say(f"{item['label']} ({item['name']}) from '{source.name}'")
        self.say(f"  image   {item['image']}@{item['digest']}")
        self.say(f"  roles   {', '.join(item['roles'])}")
        self.say("  it will be allowed:")
        for line in describe_permissions(item["permissions"]):
            self.say(f"    {line}")
        if source.credential:
            self.say(f"  the token of this source will be sent to {item['registry']} to pull the image")
        if args.dry_run:
            self.say("Dry run: nothing was installed.")
            return 0
        if not self.confirm(f"Install {item['name']} with these permissions?", args.yes):
            return 2
        self.manager.install(source.id, item["name"], item["digest"], approved_by=None)
        self.say(f"Installed {item['name']}. The gateway has been told.")
        return 0

    def update(self, args) -> int:
        row, offer = self.manager.available_update(args.name)
        if offer is None:
            self.say(f"{args.name} is already what its catalog offers.")
            return 0
        if not offer.entry.compatible:
            raise SourceError("incompatible_provider", f"'{args.name}' {offer.entry.incompatibility}", 422)
        self.say(f"{row.label} ({row.name}): {row.digest} -> {offer.entry.digest}")
        changes = describe_diff(offer.diff.to_dict())
        if changes:
            self.say("  what changes in what you approved:")
            for line in changes:
                self.say(f"    {line}")
            if offer.diff.grants_more:
                self.say("  this update asks for MORE than you approved before.")
        else:
            self.say("  the permissions are the same as the ones you approved.")
        if args.dry_run:
            self.say("Dry run: nothing was changed.")
            return 0
        if not self.confirm(f"Update {args.name} to {offer.entry.digest}?", args.yes):
            return 2
        self.manager.update(args.name, offer.entry.digest, approved_by=None)
        self.say(f"Updated {args.name}. The gateway has been told.")
        return 0

    def uninstall(self, args) -> int:
        row = self.manager.get_installed(args.name)
        if args.dry_run:
            self.say(f"Dry run: would uninstall {row.name} ({row.image}@{row.digest}).")
            return 0
        self.manager.uninstall(args.name)
        self.say(f"Uninstalled {args.name}. The gateway has been told.")
        return 0


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[[], Session] | None = None,
    manager_factory: Callable[[Session], SourceManager] = SourceManager,
    out: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    out, stdin = out or sys.stdout, stdin or sys.stdin
    environ = os.environ if environ is None else environ
    try:
        credential_cipher.require_credentials_key()  # a source's token is stored sealed (docs/credentials.md)
    except credential_cipher.CredentialsKeyError as refused:
        print(f"error: credentials_key: {refused}", file=out)
        return 1
    if session_factory is None:
        from db.engine import engine
        from services.database_init import init_db

        init_db()
        session_factory = lambda: Session(engine)  # noqa: E731
    with session_factory() as session:
        cli = Cli(manager_factory(session), out, environ, stdin)
        try:
            return getattr(cli, args.command.replace("-", "_"))(args)
        except SourceError as exc:
            print(f"error: {exc.code}: {exc}", file=out)
            return 1


if __name__ == "__main__":
    sys.exit(main())
