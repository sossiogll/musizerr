"""The host keeps the gateway's providers equal to the database's: at start, after a change, and after the gateway restarted."""
from __future__ import annotations

import json

import pytest
from sqlmodel import Session

from db.models import InstalledProvider, ProviderSource
from services import provider_events
from services.folders import FoldersNotReady
from services.gateway_client import GatewayClient
from services.gateway_sync import GatewaySync, desired_entries, generation
from tests.sources_support import DIGEST_A, DIGEST_B, TOKEN, FakeControlSocket


class Registry:
    gateway_key = None


@pytest.fixture
def gateway():
    fake = FakeControlSocket()
    yield fake
    fake.close()


def install(engine, name="lrclib", digest=DIGEST_A, credential=None, insecure=False, roles=("lyrics",)):
    with Session(engine) as session:
        source = ProviderSource(name=f"s-{name}", url=f"https://{name}.example/c.json", allow_insecure_registry=insecure)
        source.set_credential(credential)
        session.add(source)
        session.flush()
        session.add(InstalledProvider(
            source_id=source.id, name=name, image=f"ghcr.io/o/{name}", digest=digest, provider_api_version=6, label=name,
            roles=json.dumps(list(roles)), permissions=json.dumps({"egress": ["a.example:443"]}),
        ))
        session.commit()


def test_the_desired_set_is_what_the_database_says_in_a_stable_order(engine):
    install(engine, "zeta", credential=TOKEN)
    install(engine, "alpha", insecure=True)

    with Session(engine) as session:
        entries = desired_entries(session)

    assert [e["name"] for e in entries] == ["alpha", "zeta"]
    assert entries[0]["insecure"] is True and "credential" not in entries[0]
    assert entries[1]["credential"] == TOKEN and entries[1]["insecure"] is False
    assert entries[1]["permissions"] == {"egress": ["a.example:443"]} and entries[1]["roles"] == ["lyrics"]


def test_the_generation_changes_with_anything_the_gateway_acts_on_but_not_with_the_token(engine):
    install(engine, "one", credential=TOKEN)
    with Session(engine) as session:
        base = desired_entries(session)

    other_token = [{**e, "credential": "something-else"} for e in base]
    other_digest = [{**e, "digest": DIGEST_B} for e in base]
    other_permissions = [{**e, "permissions": {"egress": ["b.example:443"]}} for e in base]

    assert generation(base) == generation(other_token)
    assert len({generation(base), generation(other_digest), generation(other_permissions), generation([])}) == 4
    assert TOKEN not in generation(base)


def test_the_watcher_pushes_the_set_to_a_gateway_that_does_not_have_it_and_then_leaves_it_alone(engine, gateway):
    install(engine)
    client = GatewayClient(gateway.path)
    sync = GatewaySync(client, lambda: Session(engine))
    watcher = provider_events.GatewayWatcher(Registry(), client, lambda: None, sync=sync)

    watcher.check()
    assert len(gateway.pushes()) == 1 and gateway.pushes()[0]["providers"][0]["name"] == "lrclib"
    watcher.check()
    assert len(gateway.pushes()) == 1, "the gateway has it now"

    gateway.restart()
    watcher.check()
    assert len(gateway.pushes()) == 2, "a restarted gateway knows nothing"

    install(engine, "second")
    watcher.check()
    assert len(gateway.pushes()) == 3 and {p["name"] for p in gateway.pushes()[-1]["providers"]} == {"lrclib", "second"}


def test_a_database_that_fails_does_not_stop_the_watcher(gateway):
    client = GatewayClient(gateway.path)

    class Broken(GatewaySync):
        def ensure(self, listing):
            raise RuntimeError("database is locked")

    announced = []
    watcher = provider_events.GatewayWatcher(Registry(), client, lambda: announced.append(1), sync=Broken(client))

    assert watcher.check() is True and announced == [1]


def test_a_gateway_that_is_not_there_is_not_an_error(engine, tmp_path):
    sync = GatewaySync(GatewayClient(tmp_path / "absent.sock"), lambda: Session(engine))

    result = sync.push()

    assert result.pushed is False and "does not answer" in result.error


def test_the_pushed_credential_is_not_in_the_log(engine, gateway, caplog):
    caplog.set_level("DEBUG")
    install(engine, credential=TOKEN)
    gateway.install_reply = {"ok": False, "error": "unknown operation"}

    result = GatewaySync(GatewayClient(gateway.path), lambda: Session(engine)).push()

    assert result.pushed is False and TOKEN not in caplog.text and TOKEN not in result.error


def test_the_api_tells_the_gateway_what_is_installed_before_it_builds_its_registry(monkeypatch):
    from routes import deps

    events: list[str] = []

    class Sync:
        client = type("Client", (), {"deployed": staticmethod(lambda: True)})()

        def push(self):
            events.append("push")

    class Registry:
        def __init__(self):
            events.append("registry")

    class Background:
        def __init__(self, *args, **kwargs):
            events.append(type(self).__name__)

        def start(self):
            return self

    monkeypatch.setattr(deps, "GatewaySync", Sync)
    monkeypatch.setattr(deps, "ConfigManager", lambda: object())
    monkeypatch.setattr(deps, "ProviderRegistry", Registry)
    monkeypatch.setattr(deps, "seal_declared_secrets", lambda registry: events.append("seal"))
    monkeypatch.setattr(deps, "ProviderReloader", type("ProviderReloader", (Background,), {}))
    monkeypatch.setattr(deps, "GatewayWatcher", type("GatewayWatcher", (Background,), {}))
    monkeypatch.setattr(deps, "_registry", None)
    monkeypatch.setattr(deps, "_config", None)

    deps.init_singletons()

    assert events == ["push", "registry", "seal", "ProviderReloader", "GatewayWatcher"]


def test_without_a_gateway_the_api_starts_without_pushing(monkeypatch):
    from routes import deps

    class Sync:
        client = type("Client", (), {"deployed": staticmethod(lambda: False)})()

        def push(self):
            raise AssertionError("pushed to a gateway that is not there")

    monkeypatch.setattr(deps, "GatewaySync", Sync)
    monkeypatch.setattr(deps, "ConfigManager", lambda: object())
    monkeypatch.setattr(deps, "ProviderRegistry", lambda: object())
    monkeypatch.setattr(deps, "seal_declared_secrets", lambda registry: None)
    monkeypatch.setattr(deps, "ProviderReloader", lambda registry, after_reload=None: type("R", (), {"start": lambda self: self})())
    monkeypatch.setattr(deps, "GatewayWatcher", lambda registry, sync=None: type("W", (), {"start": lambda self: self})())
    monkeypatch.setattr(deps, "_registry", None)
    monkeypatch.setattr(deps, "_config", None)

    deps.init_singletons()


def test_the_watcher_says_once_that_the_host_and_the_gateway_name_different_working_folders(gateway, caplog):
    client = GatewayClient(gateway.path)
    folder = ["/data/work"]
    watcher = provider_events.GatewayWatcher(
        Registry(), client, lambda: None, workspace_dir=lambda: folder[0], convert=lambda: None,
    )

    watcher.check()
    watcher.check()

    messages = [r.getMessage() for r in caplog.records if "working folder" in r.getMessage()]
    assert len(messages) == 1, "said once per pair of values, not every few seconds"
    assert "/data/work" in messages[0] and "/downloads/.musizeer-work" in messages[0]
    assert "MUSIZEER_" not in messages[0], "no variable names the folder any more"

    folder[0] = "/downloads/.musizeer-work/"  # the same folder, spelled with a trailing slash
    caplog.clear()
    watcher.check()
    assert not [r for r in caplog.records if "working folder" in r.getMessage()]

    folder[0] = "/data/work"  # a desync after a good time is said again
    watcher.check()
    assert len([r for r in caplog.records if "working folder" in r.getMessage()]) == 1


def test_the_watcher_compares_nothing_while_the_working_folder_is_not_set(gateway, caplog):
    def not_set():
        raise FoldersNotReady("working_folder_not_set", "not set")

    watcher = provider_events.GatewayWatcher(
        Registry(), GatewayClient(gateway.path), lambda: None, workspace_dir=not_set, convert=lambda: None,
    )

    watcher.check()

    assert not [r for r in caplog.records if "working folder" in r.getMessage()]
