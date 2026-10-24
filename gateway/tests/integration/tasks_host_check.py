"""
The host's side of scripts/test-gateway-tasks-integration.sh: in a container of the application image, with the gateway's
socket volume mounted where the application has it. It uses the application's own code (``ProviderRegistry``,
``GatewayClient``) against the REAL gateway and the sample image gateway/tests/fixtures/tasks-provider (a provider
that starts threads on request, and declares ``limits.processes`` = 100 and keeps the default
memory limit). One mode per run:

  shared-uid  the gateway shares its uid with a container that holds more tasks than the old ``--nproc`` allowed: the
              provider still runs, its sandbox has no RLIMIT_NPROC of its own and it starts its threads
              (prints SHARED_UID_OK); with the default memory limit and the default 8 MB stacks it starts 64 threads, and how
              many it can start at most is printed (THREADS_OK; before MALLOC_ARENA_MAX=2 it was 14 on a 10-CPU host);
  watchdog    a provider that holds more tasks than its limit is stopped by the gateway, which says why in ``list``
              (the kernel did not stop it: it got every thread it asked for) and runs it again (prints WATCHDOG_OK). It starts
              by waiting for the sandbox to drain the previous check's burst (``settle``), which fills its address space;
  pids        in a gateway whose container has a small pids limit, a provider that cannot start a thread is reported:
              ``list`` counts it, ``container`` shows the limit (prints PIDS_OK; the script reads the gateway's log).

Exits non-zero on the first thing that is wrong.
"""
from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("MUSIZEER_DATA_DIR", "/tmp/data")

from services.database_init import init_db  # noqa: E402

from musizeer_sdk.auth import AuthSession  # noqa: E402
from musizeer_sdk.models import Track  # noqa: E402
from services.gateway_client import GatewayClient, GatewayUnreachable  # noqa: E402
from services.provider_proxy import ProviderCallFailed  # noqa: E402
from services.provider_registry import ProviderRegistry  # noqa: E402

SESSION = AuthSession.anonymous()
LIMIT = 100
DEFAULT_MEMORY = 1024 * 1024 * 1024  # the address space a sandbox gets when its manifest says nothing (limits.memory_mb)


def say(marker: str, **fields) -> None:
    print(marker, json.dumps(fields) if fields else "", flush=True)


def fail(message: str) -> None:
    print("FAILED:", message, flush=True)
    sys.exit(1)


def expect(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def ask(provider, command: str) -> dict:
    return json.loads(provider.get_lyrics(Track(title=command, artist="x", album="y", duration_ms=1000), SESSION).plain)


def settle(provider) -> dict:
    """Wait until the threads of the previous burst are gone, and return the provider's ``info``.

    The burst that ends ``shared-uid`` ("as many as fit") runs the sandbox out of address space on purpose, and its threads live
    a second. The runner serves every call on a thread of its own, so for that second it cannot answer: the call that comes next
    (the first of ``watchdog``, when the two checks follow each other) is dropped with ``RuntimeError: can't start new thread``
    in the runner and "closed the connection" at the host, which is the sandbox's address space doing its job and not what
    ``watchdog`` is about. So a call that fails while the burst drains is asked again."""
    deadline = time.monotonic() + 30
    while True:
        try:
            info = ask(provider, "info")
            if info["threads"] < 10:
                return info
        except ProviderCallFailed as exc:
            if time.monotonic() > deadline:
                fail(f"the provider did not recover from the previous burst: {exc}")
        if time.monotonic() > deadline:
            fail(f"the threads of the previous burst did not end: {info}")
        time.sleep(0.5)


def listing() -> dict:
    """The gateway's listing. While a burst holds every task of a container with a small pids limit the gateway cannot start the
    thread that answers either (the limit is shared by the whole container): ask again."""
    deadline = time.monotonic() + 20
    while True:
        try:
            return GatewayClient().request({"op": "list"})
        except GatewayUnreachable:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def view() -> dict:
    (tasks,) = [p for p in listing()["providers"] if p["name"] == "tasks"]
    return tasks


def provider_or_fail():
    registry = ProviderRegistry()
    provider = registry.get_lyrics_provider("tasks")
    if provider is None:
        fail(f"tasks is not in the registry: {registry.lyrics_names()} {registry.rejected()}")
    return provider


def shared_uid() -> None:
    provider = provider_or_fail()
    info = ask(provider, "info")
    expect(info["nproc"][0] != LIMIT and info["nproc"][0] > LIMIT, f"the sandbox runs under an RLIMIT_NPROC of its own: {info}")
    expect(info["as"][0] == DEFAULT_MEMORY, f"the fixture is meant to run under the default memory limit: {info}")
    expect(info["arena_max"] == "2", f"glibc is not held to two malloc arenas in the sandbox: {info}")
    # Threads that live 1 s: shorter than the gateway's count of the sandbox (every 2 s), so this is a burst, never a leak.
    result = ask(provider, "threads:64:1")
    expect(result["error"] is None and result["started"] == 64, f"the provider could not start 64 threads under the default memory limit: {result}")
    expect(result["threads"] >= 65, f"it does not hold the threads it started: {result}")
    say("SHARED_UID_OK", nproc=info["nproc"], started=result["started"], holds=result["threads"], arena_max=info["arena_max"])
    time.sleep(2)
    most = ask(provider, "threads:600:1")  # as many as it can: the address space (1024 MB) ends it, with a message
    expect(most["started"] >= 64, f"fewer than 64 threads fit under the default memory limit: {most}")
    say("THREADS_OK", most_threads_started=most["started"], stopped_by=most["error"])
    settle(provider)  # its threads live a second, and the address space is full until they end


def watchdog() -> None:
    provider = provider_or_fail()
    before = settle(provider)  # the threads of the previous check are gone, and so is their hold on the address space
    restarts_before = view()["restarts"]
    result = ask(provider, "threads:120:120:256")
    expect(result["error"] is None and result["started"] == 120, f"the kernel stopped the threads (it must not): {result}")
    deadline = time.monotonic() + 30
    stopped = None
    while time.monotonic() < deadline:
        state = view()
        if state["restarts"] > restarts_before and "process/thread limit reached" in state["reason"]:
            stopped = state
            break
        time.sleep(0.5)
    expect(stopped is not None, f"the gateway did not stop the provider: {view()}")
    expect(stopped["tasks"]["limit"] == LIMIT and stopped["tasks"]["peak"] > LIMIT, f"the count: {stopped['tasks']}")
    expect(f"over its limit of {LIMIT}" in stopped["reason"], f"the reason: {stopped['reason']}")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and view()["state"] != "running":
        time.sleep(0.5)
    expect(view()["state"] == "running", f"it did not come back: {view()}")
    after = ask(provider_or_fail(), "info")
    expect(after["instance"] != before["instance"], "it is the same process: it was not restarted")
    say("WATCHDOG_OK", reason=stopped["reason"], peak=stopped["tasks"]["peak"], restarts=stopped["restarts"])


def pids() -> None:
    provider = provider_or_fail()
    document = listing()
    container = document["container"]
    expect(container is not None and container["pids_limit"] is not None, f"the gateway does not see its pids limit: {container}")
    result = ask(provider, "threads:400:4")
    expect(result["error"] is not None and "can't start new thread" in result["error"], f"the container's pids limit did not stop the threads: {result}")
    expect(result["started"] < 400, f"all the threads started: {result}")
    deadline = time.monotonic() + 15
    hits = 0
    while time.monotonic() < deadline:
        hits = view()["task_limit_hits"]["count"]
        if hits:
            break
        time.sleep(0.3)
    expect(hits >= 1, f"the gateway did not notice the provider could not start a thread: {view()}")
    expect("RLIMIT_NPROC / pids" in view()["task_limit_hits"]["last"], f"what it recorded: {view()['task_limit_hits']}")
    say("PIDS_OK", pids_limit=container["pids_limit"], started=result["started"], hits=hits)


def main() -> None:
    init_db()
    modes = {"shared-uid": shared_uid, "watchdog": watchdog, "pids": pids}
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in modes:
        fail(f"usage: tasks_host_check.py {'|'.join(modes)}")
    modes[mode]()


if __name__ == "__main__":
    main()
