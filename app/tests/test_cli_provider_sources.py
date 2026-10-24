"""
``python -m scripts.provider_sources``: the same services as the routes from the host's shell. A token
comes from an environment variable and never from the command line, and is never printed.
"""
from __future__ import annotations

import io
import json

import pytest
from sqlmodel import Session, select

from db.models import InstalledProvider
from scripts import provider_sources as cli
from services.gateway_client import GatewayClient
from services.gateway_sync import GatewaySync
from services.sources import CatalogFetcher, SourceManager
from tests.sources_support import DIGEST_A, DIGEST_B, TOKEN, FakeCatalogServer, FakeControlSocket, catalog, entry

URL = "https://catalog.example/registry.json"
PRIVATE_URL = "https://private.example/catalog.json"


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def world(engine):
    server, gateway = FakeCatalogServer(), FakeControlSocket()
    world = type("World", (), {})()
    world.server, world.gateway, world.engine, world.announced = server, gateway, engine, []

    def run(*argv, stdin="", environ=None, tty=False):
        out = io.StringIO()
        code = cli.main(
            list(argv),
            session_factory=lambda: Session(engine),
            manager_factory=lambda s: SourceManager(
                s, announce_change=lambda: world.announced.append(1),
                gateway=GatewaySync(GatewayClient(gateway.path), lambda: Session(engine)), fetcher=CatalogFetcher(server.transport),
            ),
            out=out, environ=environ or {}, stdin=(Terminal if tty else io.StringIO)(stdin),
        )
        return code, out.getvalue()

    world.run = run
    yield world
    gateway.close()


def installed(world):
    with Session(world.engine) as session:
        return session.exec(select(InstalledProvider)).all()


def test_listing_shows_the_sources_and_what_is_installed_without_the_network(world):
    world.server.serve(URL, catalog(entry(), name="Official"))
    world.run("add-source", URL)
    world.run("install", "Official", "lrclib", "--yes")
    requests = len(world.server.requests)

    code, text = world.run("list")

    assert code == 0 and "Official" in text and URL in text and "lrclib" in text and DIGEST_A in text
    assert len(world.server.requests) == requests


def test_adding_a_source_with_a_dry_run_stores_nothing(world):
    world.server.serve(URL, catalog(entry(), entry("two")))

    code, text = world.run("add-source", URL, "--dry-run")

    assert code == 0 and "usable catalog with 2 providers" in text
    assert world.run("list")[1].startswith("No sources.")


def test_a_token_is_read_from_the_environment_and_never_printed(world):
    world.server.require_token[PRIVATE_URL] = TOKEN
    world.server.serve(PRIVATE_URL, catalog(entry()))

    code, text = world.run("add-source", PRIVATE_URL, "--credential-env", "MY_TOKEN", environ={"MY_TOKEN": f" {TOKEN}\n"})

    assert code == 0 and "with a token" in text and TOKEN not in text
    assert world.server.authorizations() == [(PRIVATE_URL, f"Bearer {TOKEN}")]
    listing = world.run("list")[1]
    assert "token set" in listing and TOKEN not in listing
    world.run("install", PRIVATE_URL, "lrclib", "--yes")
    assert world.gateway.pushes()[-1]["providers"][0]["credential"] == TOKEN


def test_a_token_cannot_be_given_on_the_command_line(world, capsys):
    for argv in (
        ["add-source", PRIVATE_URL, "--credential", TOKEN],
        ["add-source", PRIVATE_URL, "--credential=" + TOKEN],
        ["add-source", PRIVATE_URL, "--cred", TOKEN],
        ["add-source", PRIVATE_URL, "--token", TOKEN],
        ["edit-source", "x", "--credential", TOKEN],
    ):
        with pytest.raises(SystemExit) as exit_:
            world.run(*argv)
        assert exit_.value.code == 2

    assert world.server.requests == []
    heard = capsys.readouterr()
    assert TOKEN not in heard.out and TOKEN not in heard.err, "not even echoed by the usage message"
    assert "--credential-env" in heard.err


def test_the_option_that_names_the_variable_takes_a_name_not_a_token(world):
    code, text = world.run("add-source", PRIVATE_URL, "--credential-env", "NOT_SET", environ={})

    assert code == 1 and "invalid_credential" in text and "NOT_SET" in text and world.server.requests == []


def test_a_refusal_prints_its_code_and_exits_1(world):
    world.server.serve(URL, {"name": "x", "version": 1, "providers": []})

    code, text = world.run("add-source", URL)

    assert code == 1 and text.startswith("error: catalog_invalid:") and "version 1" in text


def test_the_catalog_command_shows_every_entry_and_what_cannot_be_installed(world):
    world.server.serve(URL, catalog(entry(), entry("old", provider_api_version=1), entry("broken", digest="x")))
    world.run("add-source", URL)

    code, text = world.run("catalog", URL)

    assert code == 0 and "lrclib" in text and "[not installed]" in text
    assert "cannot be installed here" in text and "! broken: not valid" in text


def test_install_prints_the_permissions_and_asks_first(world):
    world.server.serve(URL, catalog(entry(permissions={"egress": ["lrclib.net:443"], "egress_from_settings": ["url"], "limits": {"memory_mb": 256}})))
    world.run("add-source", URL)

    declined = world.run("install", URL, "lrclib", stdin="n\n", tty=True)

    assert declined[0] == 2 and installed(world) == [] and "Nothing was written" in declined[1]
    assert "network: lrclib.net:443" in declined[1] and "the setting 'url'" in declined[1] and "memory mb 256" in declined[1]
    assert f"ghcr.io/owner/musizeer-provider-lrclib@{DIGEST_A}" in declined[1]

    approved = world.run("install", URL, "lrclib", stdin="y\n", tty=True)

    assert approved[0] == 0 and [row.name for row in installed(world)] == ["lrclib"]
    assert world.gateway.pushes()[-1]["providers"][0]["name"] == "lrclib" and world.announced


def test_without_a_terminal_and_without_yes_nothing_is_installed(world):
    world.server.serve(URL, catalog(entry()))
    world.run("add-source", URL)

    code, text = world.run("install", URL, "lrclib", stdin="y\n")

    assert code == 2 and installed(world) == [] and "use --yes" in text


def test_install_dry_run_and_the_token_disclosure(world):
    world.server.require_token[PRIVATE_URL] = TOKEN
    world.server.serve(PRIVATE_URL, catalog(entry()))
    world.run("add-source", PRIVATE_URL, "--credential-env", "T", environ={"T": TOKEN})

    code, text = world.run("install", PRIVATE_URL, "lrclib", "--dry-run")

    assert code == 0 and installed(world) == [] and "Dry run" in text
    assert "token of this source will be sent to ghcr.io" in text and TOKEN not in text


def test_install_refuses_what_is_not_in_the_catalog_or_not_compatible(world):
    world.server.serve(URL, catalog(entry("old", provider_api_version=1)))
    world.run("add-source", URL)

    assert "provider_not_in_catalog" in world.run("install", URL, "ghost", "--yes")[1]
    assert "incompatible_provider" in world.run("install", URL, "old", "--yes")[1]
    assert installed(world) == []


def test_update_shows_the_difference_and_asks(world):
    world.server.serve(URL, catalog(entry()))
    world.run("add-source", URL)
    world.run("install", URL, "lrclib", "--yes")
    world.server.serve(URL, catalog(entry("lrclib", DIGEST_B, permissions={"egress": ["lrclib.net:443", "evil.example:443"], "limits": {"memory_mb": 512}})))

    dry = world.run("update", "lrclib", "--dry-run")
    assert dry[0] == 0 and "+ network: evil.example:443" in dry[1] and "limit memory mb: default -> 512" in dry[1]
    assert "asks for MORE" in dry[1] and installed(world)[0].digest == DIGEST_A

    refused = world.run("update", "lrclib", stdin="no\n", tty=True)
    assert refused[0] == 2 and installed(world)[0].digest == DIGEST_A

    done = world.run("update", "lrclib", "--yes")
    assert done[0] == 0 and installed(world)[0].digest == DIGEST_B
    assert "already what its catalog offers" in world.run("update", "lrclib", "--yes")[1]


def test_uninstall_has_a_dry_run(world):
    world.server.serve(URL, catalog(entry()))
    world.run("add-source", URL)
    world.run("install", URL, "lrclib", "--yes")

    assert "would uninstall lrclib" in world.run("uninstall", "lrclib", "--dry-run")[1] and len(installed(world)) == 1
    assert world.run("uninstall", "lrclib")[0] == 0 and installed(world) == []
    assert world.gateway.pushes()[-1]["providers"] == []
    assert world.run("uninstall", "lrclib")[0] == 1


def test_edit_source_changes_the_registry_mode_the_token_and_the_state(world):
    world.server.serve(URL, catalog(entry(), name="Official"))
    world.run("add-source", URL)

    assert world.run("edit-source", "Official", "--insecure", "--disable")[0] == 0
    listing = world.run("list")[1]
    assert "disabled" in listing and "insecure registry" in listing
    assert world.run("edit-source", "Official", "--secure", "--enable")[0] == 0
    assert "(" not in world.run("list")[1].splitlines()[1].split("]")[-1]

    world.server.require_token[URL] = TOKEN
    assert world.run("edit-source", "Official", "--credential-env", "T", environ={"T": TOKEN})[0] == 0
    assert "token set" in world.run("list")[1]
    assert world.run("edit-source", "Official", "--clear-credential")[0] == 0
    assert "token set" not in world.run("list")[1]
    assert "source_not_found" in world.run("edit-source", "ghost", "--enable")[1]


def test_the_description_of_permissions_is_in_words():
    assert cli.describe_permissions({"egress": [], "egress_from_settings": [], "filesystem_from_settings": [], "limits": {}}) == [
        "no network, no files, the gateway's default limits"
    ]
    lines = cli.describe_permissions({
        "egress": ["a.example:443"], "egress_from_settings": ["url"],
        "filesystem_from_settings": [{"setting": "root_dir", "mode": "rw"}, {"setting": "cache", "mode": "ro"}],
        "limits": {"memory_mb": 64, "open_files": None},
    })
    assert lines[0] == "network: a.example:443"
    assert any("read and write" in line for line in lines) and any("read only" in line for line in lines)
    assert lines[-1] == "limits: memory mb 64"


def test_install_says_wildcards_and_raw_tcp_in_words(world):
    world.server.serve(URL, catalog(entry(permissions={
        "egress": ["*.cdn.example:443"], "tcp_from_settings": [{"setting": "host", "ports": "any"}, {"setting": "alt", "ports": "30000-30100"}],
    })))
    world.run("add-source", URL)

    code, text = world.run("install", URL, "lrclib", "--dry-run")

    assert code == 0
    assert "network: *.cdn.example:443 (any subdomain, not cdn.example itself)" in text
    assert "network: any port of the host the setting 'host' names" in text
    assert "network: port 30000-30100 of the host the setting 'alt' names" in text
