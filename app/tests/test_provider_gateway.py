"""The host and the provider gateway: which providers the registry gets from it, and what it tells it."""
from __future__ import annotations

import logging

import pytest

from musizerr_sdk.auth import AuthSession
from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.models import Track
from musizerr_sdk.permissions import FilesystemAccess, Permissions, TcpAccess
from services import gateway_client, provider_events
from services.gateway_client import EgressGrants, GatewayClient, GatewayProviderContext, GatewayUnreachable
from services.provider_registry import ProviderRegistry

from tests.gateway_support import PERMISSIONS, FakeGateway, Remote, declared, gateway  # noqa: F401 — `gateway` is a fixture

SESSION = AuthSession.anonymous()
TRACK = Track(title="Song", artist="Artist", album="Album", duration_ms=1000)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(GatewayProviderContext, "settings", lambda self: {"url": "https://music.example.org"})


def registry_for(gateway: FakeGateway) -> ProviderRegistry:
    return ProviderRegistry(gateway=GatewayClient(gateway.run_dir / "gateway.sock"))


# ------------------------------------------------------------------
# The registry
# ------------------------------------------------------------------


def test_a_provider_the_gateway_runs_is_in_the_registry(gateway):
    gateway.serve(Remote, "remote")

    registry = registry_for(gateway)

    assert registry.get_lyrics_provider("remote") is not None and registry.lyrics_names() == ["remote"]
    lyrics = registry.get_lyrics_provider("remote").get_lyrics(TRACK, SESSION)
    assert lyrics.plain == "lyrics of Song for https://music.example.org", "its settings reach it with the call"
    assert registry.rejected() == {}
    assert registry.gateway_key == ("e1", 1)


def test_a_provider_that_is_not_running_is_not_registered(gateway):
    gateway.serve(Remote, "remote", state="restarting")
    gateway.serve(Remote, "other", state="refused")

    registry = registry_for(gateway)

    assert registry.lyrics_names() == [] and registry.rejected() == {}



@pytest.mark.parametrize("what, declaration", [
    ("name", declared(name="someone-else")),
    ("roles", declared(roles=("metadata",))),
    ("provider API version", declared(version=PROVIDER_API_VERSION + 1)),
    ("permissions", declared(permissions=Permissions(egress=("evil.example:443",)))),
])
def test_a_provider_that_says_something_else_than_its_image_is_refused(gateway, what, declaration):
    gateway.serve(Remote, "remote", declaration=declaration)

    registry = registry_for(gateway)

    assert registry.get_lyrics_provider("remote") is None
    assert what in registry.rejected()["remote"] and "differs from its image" in registry.rejected()["remote"]


def test_a_provider_that_cannot_describe_itself_is_refused(gateway):
    def broken(context):
        raise RuntimeError("cannot start")

    gateway.serve(broken, "remote")

    registry = registry_for(gateway)

    assert registry.get_lyrics_provider("remote") is None and "did not describe itself" in registry.rejected()["remote"]


@pytest.mark.parametrize("name", ["../etc", "Upper", "a/b", ""])
def test_a_name_that_cannot_be_a_folder_is_refused(gateway, name):
    gateway.serve(Remote, "remote", declaration=declared("remote"))
    gateway.providers[0]["name"] = name

    registry = registry_for(gateway)

    assert registry.lyrics_names() == []


def test_without_a_gateway_the_registry_starts_empty_and_says_so(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        registry = ProviderRegistry(gateway=GatewayClient(tmp_path / "gateway.sock"))

    assert registry.metadata_names() == [] and registry.gateway_key is None
    assert "Starting without the providers of the gateway" in caplog.text


def test_a_gateway_that_is_there_but_not_answering_is_a_warning_not_a_crash(tmp_path, caplog):
    (tmp_path / "gateway.sock").write_text("not a socket")

    with caplog.at_level(logging.WARNING):
        registry = ProviderRegistry(gateway=GatewayClient(tmp_path / "gateway.sock"))

    assert registry.metadata_names() == []
    assert any(r.levelno == logging.WARNING and "gateway" in r.message for r in caplog.records)


def test_a_listing_that_is_malformed_is_the_same_as_no_gateway(gateway):
    gateway.answer = lambda request: {"ok": True, "epoch": "e", "generation": "many", "providers": 5}  # type: ignore[method-assign]

    assert registry_for(gateway).gateway_key is None


def test_reload_picks_up_a_provider_that_came_up_and_never_leaves_the_registry_half_built(gateway):
    gateway.serve(type("First", (Remote,), {"name": "first"}), "first")
    registry = registry_for(gateway)
    before = registry._by_role["lyrics"]  # noqa: SLF001
    assert registry.lyrics_names() == ["first"]
    gateway.serve(type("Second", (Remote,), {"name": "second"}), "second")
    gateway.generation = 2

    registry.reload()

    assert registry.lyrics_names() == ["first", "second"] and registry.gateway_key == ("e1", 2)
    assert set(before) == {"first"}, "readers of the old registry keep a whole one"


def test_a_failed_reload_keeps_the_registry_it_has(gateway, monkeypatch):
    gateway.serve(Remote, "remote")
    registry = registry_for(gateway)
    monkeypatch.setattr(ProviderRegistry, "_load_gateway", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        registry.reload()

    assert registry.lyrics_names() == ["remote"]


# ------------------------------------------------------------------
# What the host tells the gateway
# ------------------------------------------------------------------


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_the_destinations_the_settings_name_are_sent_before_the_call_and_not_again_until_they_change(gateway):
    gateway.serve(Remote, "remote")
    registry = registry_for(gateway)
    provider = registry.get_lyrics_provider("remote")

    provider.get_lyrics(TRACK, SESSION)
    provider.get_lyrics(TRACK, SESSION)

    grants = [r for r in gateway.requests if r["op"] == "egress"]
    assert grants == [{"op": "egress", "provider": "remote", "targets": ["music.example.org:443"], "ttl": gateway_client.GRANT_TTL}]


def test_a_changed_setting_is_sent_at_once_and_a_grant_is_renewed_before_it_runs_out(gateway, monkeypatch):
    clock = Clock()
    client = GatewayClient(gateway.run_dir / "gateway.sock")
    grants = EgressGrants(client, clock)
    settings = {"url": "http://192.168.1.5:4533"}
    monkeypatch.setattr(GatewayProviderContext, "settings", lambda self: dict(settings))
    context = GatewayProviderContext("remote", PERMISSIONS, grants)

    context.snapshot()
    context.snapshot()
    settings["url"] = "http://192.168.1.6:4533"
    context.snapshot()
    clock.now = gateway_client.GRANT_REFRESH_AFTER + 1
    context.snapshot()

    assert [r["targets"] for r in gateway.requests if r["op"] == "egress"] == [
        ["192.168.1.5:4533"], ["192.168.1.6:4533"], ["192.168.1.6:4533"],
    ]


def test_a_provider_that_names_no_setting_never_asks_for_a_grant(gateway):
    context = GatewayProviderContext("remote", Permissions(egress=("api.example.org:443",)), EgressGrants(GatewayClient(gateway.run_dir / "gateway.sock")))

    context.snapshot()

    assert [r for r in gateway.requests if r["op"] == "egress"] == []


def test_a_gateway_that_cannot_be_told_does_not_stop_the_call(tmp_path, caplog):
    grants = EgressGrants(GatewayClient(tmp_path / "gone.sock"))
    context = GatewayProviderContext("remote", PERMISSIONS, grants)

    with caplog.at_level(logging.WARNING):
        snapshot = context.snapshot()

    assert snapshot.settings == {"url": "https://music.example.org"} and "could not tell the gateway" in caplog.text


def test_the_client_reads_the_gateways_answers_and_reports_what_it_cannot(gateway, tmp_path):
    client = GatewayClient(gateway.run_dir / "gateway.sock")
    gateway.serve(Remote, "remote")

    listing = client.listing()
    assert (listing.epoch, listing.generation, listing.workspace_root) == ("e1", 1, "/downloads/.musizerr-work")
    assert listing.providers[0].name == "remote" and listing.providers[0].socket == "providers/remote/provider.sock"
    assert client.run_dir == gateway.run_dir
    with pytest.raises(GatewayUnreachable, match="refused"):
        client.request({"op": "nope"})
    with pytest.raises(GatewayUnreachable, match="does not answer"):
        GatewayClient(tmp_path / "gone.sock").listing()


# ------------------------------------------------------------------
# Across processes
# ------------------------------------------------------------------


class Registry:
    def __init__(self, key=None, stale=False):
        self.gateway_key = key
        self.reloaded = 0
        self.stale = stale

    def is_stale(self):
        return self.stale

    def reload(self):
        self.reloaded += 1


def test_the_watcher_announces_a_change_of_the_gateways_set_once(gateway):
    announced = []
    registry = Registry(("e1", 1))
    watcher = provider_events.GatewayWatcher(registry, GatewayClient(gateway.run_dir / "gateway.sock"), lambda: announced.append(1))

    assert watcher.check() is False and announced == [], "nothing changed"
    gateway.generation = 2
    assert watcher.check() is True and announced == [1]
    registry.gateway_key = ("e1", 2)  # the registry reloaded
    assert watcher.check() is False and announced == [1]
    gateway.epoch = "e2"  # the gateway restarted
    assert watcher.check() is True


def test_the_watcher_says_nothing_while_the_gateway_is_down_or_redis_is(gateway, tmp_path):
    down = provider_events.GatewayWatcher(Registry(("e1", 1)), GatewayClient(tmp_path / "gone.sock"), lambda: pytest.fail("announced"))
    assert down.check() is False

    def fail():
        raise OSError("redis down")

    gateway.generation = 5
    assert provider_events.GatewayWatcher(Registry(), GatewayClient(gateway.run_dir / "gateway.sock"), fail).check() is False


class Pubsub:
    def __init__(self, script):
        self.script = list(script)
        self.closed = False

    def get_message(self, timeout=0):
        item = self.script.pop(0) if self.script else None
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def wait_for(condition, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_every_process_rebuilds_its_registry_when_it_hears_the_event(monkeypatch):
    monkeypatch.setattr(provider_events, "COALESCE_SECONDS", 0)
    registry = Registry()
    pubsub = Pubsub([None, {"data": "changed"}])
    reloader = provider_events.ProviderReloader(registry, connect=lambda: pubsub).start()
    try:
        assert wait_for(lambda: registry.reloaded == 1)
    finally:
        reloader.stop()
    assert pubsub.closed


def test_a_registry_the_gateway_already_moved_past_is_rebuilt_on_the_first_subscription():
    registry = Registry(stale=True)  # built just before the listener subscribed: the event is gone
    reloader = provider_events.ProviderReloader(registry, connect=lambda: Pubsub([])).start()
    try:
        assert wait_for(lambda: registry.reloaded == 1)
    finally:
        reloader.stop()


def test_a_current_registry_is_not_rebuilt_when_the_listener_subscribes():
    registry = Registry(stale=False)
    reloader = provider_events.ProviderReloader(registry, connect=lambda: Pubsub([None, None])).start()
    try:
        wait_for(lambda: False, timeout=0.3)  # give it the chance to (wrongly) rebuild
    finally:
        reloader.stop()
    assert registry.reloaded == 0


def test_the_registry_knows_when_the_gateways_set_is_not_the_one_it_loaded(gateway, tmp_path):
    registry = registry_for(gateway)
    assert registry.is_stale() is False
    gateway.generation = 2
    assert registry.is_stale() is True
    registry.reload()
    assert registry.is_stale() is False
    gateway.epoch = "e2"  # the gateway restarted
    assert registry.is_stale() is True
    assert ProviderRegistry(gateway=GatewayClient(tmp_path / "gone.sock")).is_stale() is False, "a gateway that is down is not a change"


def test_a_burst_of_events_is_one_rebuild(monkeypatch):
    registry = Registry()
    messages = [{"data": "changed"}] * 5
    reloader = provider_events.ProviderReloader(registry, connect=lambda: Pubsub(messages)).start()
    try:
        assert wait_for(lambda: registry.reloaded >= 1)
        import time

        time.sleep(0.2)
    finally:
        reloader.stop()
    assert registry.reloaded == 1


def test_after_a_lost_connection_the_registry_is_rebuilt_in_case_an_event_was_missed(monkeypatch):
    monkeypatch.setattr(provider_events, "RECONNECT_SECONDS", 0.01)
    registry = Registry()
    connections = iter([Pubsub([ConnectionError("lost")]), OSError("redis is down"), Pubsub([])])

    def connect():
        item = next(connections, None)
        if isinstance(item, Exception):
            raise item
        return item or Pubsub([])

    reloader = provider_events.ProviderReloader(registry, connect=connect).start()
    try:
        assert wait_for(lambda: registry.reloaded >= 1)
    finally:
        reloader.stop()


def test_a_rebuild_that_fails_does_not_stop_the_listener(monkeypatch):
    monkeypatch.setattr(provider_events, "COALESCE_SECONDS", 0)

    class Failing(Registry):
        def reload(self):
            super().reload()
            raise RuntimeError("boom")

    registry = Failing()
    reloader = provider_events.ProviderReloader(registry, connect=lambda: Pubsub([{"data": "x"}, None, {"data": "y"}])).start()
    try:
        assert wait_for(lambda: registry.reloaded >= 2)
    finally:
        reloader.stop()


def test_the_event_goes_to_redis_on_its_channel(monkeypatch):
    published = []

    class Client:
        def publish(self, channel, message): published.append((channel, message))
        def close(self): published.append("closed")

    import redis

    monkeypatch.setattr(redis, "from_url", lambda url: Client())

    provider_events.announce_changed()

    assert published == [("musizerr:providers", "changed"), "closed"]


def test_installing_or_removing_announces_the_change_and_a_failed_announcement_does_not_fail_it():
    from services.sources import SourceManager

    announced, pushed = [], []
    gateway = type("Sync", (), {"push": lambda self, session: pushed.append(session)})()
    manager = SourceManager(session=None, announce_change=lambda: announced.append(1), gateway=gateway)  # type: ignore[arg-type]
    manager._providers_changed()  # noqa: SLF001

    def fail():
        raise OSError("redis down")

    SourceManager(session=None, announce_change=fail, gateway=gateway)._providers_changed()  # type: ignore[arg-type]  # noqa: SLF001
    assert announced == [1] and len(pushed) == 2, "the gateway is told first, and a failed announcement does not stop anything"


TCP = Permissions(tcp_from_settings=(TcpAccess("host", "any"),))
FOLDER = Permissions(filesystem_from_settings=(FilesystemAccess("root_dir", "rw"),))


def test_the_hosts_and_the_folders_the_settings_name_travel_with_the_grant(gateway, monkeypatch):
    settings = {"host": "ftp://files.lan:2121/music", "root_dir": "/downloads/music", "ignored": "/etc"}
    monkeypatch.setattr(GatewayProviderContext, "settings", lambda self: dict(settings))
    client = GatewayClient(gateway.run_dir / "gateway.sock")
    context = GatewayProviderContext("remote", Permissions(
        egress_from_settings=("url",), tcp_from_settings=(TcpAccess("host", "any"),), filesystem_from_settings=(FilesystemAccess("root_dir", "rw"),),
    ), EgressGrants(client))

    context.snapshot()

    assert [r for r in gateway.requests if r["op"] == "egress"] == [{
        "op": "egress", "provider": "remote", "targets": [], "ttl": gateway_client.GRANT_TTL,
        "tcp": {"host": ["files.lan"]}, "paths": {"root_dir": "/downloads/music"},
    }], "the port of the URL is not sent: the ports are the manifest's"


def test_a_provider_with_only_folders_or_only_raw_tcp_still_asks_for_a_grant(gateway, monkeypatch):
    monkeypatch.setattr(GatewayProviderContext, "settings", lambda self: {"host": "files.lan", "root_dir": "/downloads/x"})
    client = GatewayClient(gateway.run_dir / "gateway.sock")
    GatewayProviderContext("a", TCP, EgressGrants(client)).snapshot()
    GatewayProviderContext("b", FOLDER, EgressGrants(client)).snapshot()

    grants = {r["provider"]: r for r in gateway.requests if r["op"] == "egress"}
    assert grants["a"]["tcp"] == {"host": ["files.lan"]} and "paths" not in grants["a"]
    assert grants["b"]["paths"] == {"root_dir": "/downloads/x"} and "tcp" not in grants["b"]


def test_a_changed_folder_is_sent_at_once_an_unchanged_one_only_when_the_grant_is_renewed(gateway, monkeypatch):
    clock = Clock()
    settings = {"root_dir": "/downloads/a"}
    monkeypatch.setattr(GatewayProviderContext, "settings", lambda self: dict(settings))
    context = GatewayProviderContext("remote", FOLDER, EgressGrants(GatewayClient(gateway.run_dir / "gateway.sock"), clock))

    context.snapshot()
    context.snapshot()
    settings["root_dir"] = "/downloads/b"
    context.snapshot()
    settings["root_dir"] = ""
    context.snapshot()

    assert [r.get("paths") for r in gateway.requests if r["op"] == "egress"] == [{"root_dir": "/downloads/a"}, {"root_dir": "/downloads/b"}, None], (
        "an emptied setting is told as no folder at all"
    )


def test_a_grant_that_restarts_the_provider_is_waited_for_longer_than_other_requests(gateway, monkeypatch):
    seen = []
    real = GatewayClient.request
    monkeypatch.setattr(GatewayClient, "request", lambda self, payload, timeout=None: (seen.append(timeout), real(self, payload, timeout))[1])
    monkeypatch.setattr(GatewayProviderContext, "settings", lambda self: {"root_dir": "/downloads/a"})

    GatewayProviderContext("remote", FOLDER, EgressGrants(GatewayClient(gateway.run_dir / "gateway.sock"))).snapshot()

    assert seen == [gateway_client.GRANT_TIMEOUT_SECONDS] and gateway_client.GRANT_TIMEOUT_SECONDS > gateway_client.REQUEST_TIMEOUT_SECONDS
