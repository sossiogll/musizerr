"""
Limite di concorrenza dichiarato dai download provider
(``DownloadProvider.max_concurrent_downloads``): il resolver chiede un posto
all'host prima di chiamare il provider, e l'host li conta fra i worker.
"""
from __future__ import annotations

from contextlib import contextmanager

from core.download_resolver import DownloadResolver
from services.provider_slots import RedisProviderSlots

from tests.conftest import FakeAuthManager, FakeDownloadProvider, FakeRegistry, make_track


class _Downloader(FakeDownloadProvider):
    def __init__(self, name: str, *, limit: int | None = None, result: str | None = "/tmp/out.mp3") -> None:
        super().__init__(name)
        self.max_concurrent_downloads = limit
        self._result = result
        self.calls = 0
        self.supported_identifier_types = {"isrc"}

    def download_by_id(self, identifier, workspace, session):
        self.calls += 1
        return self._result


class _RecordingSlots:
    def __init__(self, grant: bool) -> None:
        self._grant = grant
        self.requests: list[tuple[str, int]] = []

    @contextmanager
    def acquire(self, provider_name, limit):
        self.requests.append((provider_name, limit))
        yield self._grant


def _resolver(providers, slots):
    return DownloadResolver(
        registry=FakeRegistry(downloads=providers),
        auth_manager=FakeAuthManager(),
        provider_order=[p.name for p in providers],
        slots=slots,
    )


def test_a_provider_with_a_limit_asks_for_a_slot_with_its_declared_limit():
    limited = _Downloader("limited", limit=1)
    slots = _RecordingSlots(grant=True)

    result = _resolver([limited], slots).resolve(make_track(isrc="ISRC1"), lambda name: "/tmp/dest")

    assert result.success and result.provider == "limited"
    assert slots.requests == [("limited", 1)]


def test_a_provider_without_a_limit_never_asks_for_a_slot():
    free = _Downloader("free")
    slots = _RecordingSlots(grant=False)

    result = _resolver([free], slots).resolve(make_track(isrc="ISRC1"), lambda name: "/tmp/dest")

    assert result.success and slots.requests == []


def test_no_free_slot_skips_the_provider_without_calling_it_and_tries_the_next():
    limited = _Downloader("limited", limit=1)
    fallback = _Downloader("fallback")
    slots = _RecordingSlots(grant=False)

    result = _resolver([limited, fallback], slots).resolve(make_track(isrc="ISRC1"), lambda name: "/tmp/dest")

    assert limited.calls == 0
    assert result.success and result.provider == "fallback"


def test_a_busy_provider_that_nobody_else_replaces_is_retried_not_failed():
    limited = _Downloader("limited", limit=1)
    slots = _RecordingSlots(grant=False)

    result = _resolver([limited], slots).resolve(make_track(isrc="ISRC1"), lambda name: "/tmp/dest")

    assert limited.calls == 0
    assert not result.success and result.retriable is True


# ---------------------------------------------------------------------------
# RedisProviderSlots, con lock in memoria al posto di Redis
# ---------------------------------------------------------------------------


class _MemoryLocks:
    def __init__(self) -> None:
        self.held: set[str] = set()
        self.leases: dict[str, int] = {}

    def factory(self, key, lease):
        self.leases[key] = lease
        locks = self

        class _Lock:
            def acquire(self, blocking=True):
                if key in locks.held:
                    return False
                locks.held.add(key)
                return True

            def release(self):
                locks.held.discard(key)

        return _Lock()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _slots(locks, *, wait=10, lease=300):
    clock = _Clock()
    return RedisProviderSlots(
        lease_seconds=lease, wait_seconds=wait,
        lock_factory=locks.factory, clock=clock, sleep=clock.sleep,
    )


def test_up_to_limit_holders_at_once_then_the_next_waits_and_gives_up():
    locks = _MemoryLocks()
    slots = _slots(locks)

    with slots.acquire("p", 2) as first, slots.acquire("p", 2) as second:
        assert first and second
        with slots.acquire("p", 2) as third:
            assert third is False


def test_a_slot_is_released_on_exit_and_can_be_taken_again():
    locks = _MemoryLocks()
    slots = _slots(locks, wait=0)

    with slots.acquire("p", 1) as first:
        assert first
    with slots.acquire("p", 1) as again:
        assert again
    assert locks.held == set()


def test_each_slot_lock_expires_after_the_configured_lease():
    locks = _MemoryLocks()

    with _slots(locks, lease=300).acquire("p", 2):
        pass

    assert locks.leases == {"musizerr:provider-slot:p:0": 300, "musizerr:provider-slot:p:1": 300}


def test_providers_do_not_share_slots():
    locks = _MemoryLocks()
    slots = _slots(locks, wait=0)

    with slots.acquire("a", 1) as a, slots.acquire("b", 1) as b:
        assert a and b
