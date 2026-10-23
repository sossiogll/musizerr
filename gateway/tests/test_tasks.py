"""Processes and threads: how they are counted, what the container allows, and what is said when it is too few."""
from __future__ import annotations

import logging
import os
import resource
import sys
import threading
from pathlib import Path

import pytest

from musizerr_gateway import supervisor as sv, tasks
from musizerr_gateway.config import Ceilings
from musizerr_gateway.control import provider_view
from musizerr_gateway.sandbox import limits_for
from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.labels import DeclaredManifest
from musizerr_sdk.permissions import Permissions, ResourceLimits

from test_supervisor import ENTRY, Images, state_of, wait_for, world  # noqa: F401 — `world` is a fixture


def fake_proc(root: Path, processes: dict[int, tuple[int, int]]) -> Path:
    """A /proc with the given ``pid -> (ppid, threads)``, in the format of the kernel's status files."""
    for pid, (ppid, threads) in processes.items():
        directory = root / str(pid)
        directory.mkdir(parents=True)
        (directory / "status").write_text(f"Name:\tx\nPid:\t{pid}\nPPid:\t{ppid}\nThreads:\t{threads}\nVmSize:\t10 kB\n")
    (root / "self").mkdir()  # not a process
    (root / "meminfo").write_text("MemTotal: 1 kB\n")
    return root


# ------------------------------------------------------------------
# Counting the tasks of one sandbox
# ------------------------------------------------------------------


def test_the_table_reads_the_parent_and_the_threads_of_every_process(tmp_path):
    proc = fake_proc(tmp_path, {1: (0, 1), 10: (1, 3), 11: (10, 2)})

    assert tasks.read_table(proc) == {1: (0, 1), 10: (1, 3), 11: (10, 2)}


def test_a_process_that_vanishes_while_the_table_is_read_is_left_out(tmp_path):
    proc = fake_proc(tmp_path, {10: (1, 3)})
    (proc / "11").mkdir()  # no status file: it exited between the listing and the read
    (proc / "12").mkdir()
    (proc / "12" / "status").write_text("Name:\tx\n")  # a status without the fields

    assert tasks.read_table(proc) == {10: (1, 3)}
    assert tasks.read_table(tmp_path / "missing") == {}


def test_a_sandbox_is_its_whole_process_tree_threads_included():
    #  1 (gateway)
    #  └── 20 bwrap (1)
    #      └── 21 sandbox init (1)
    #          ├── 22 runner (4 threads)
    #          │   └── 24 helper (2)
    #          └── 23 other (1)
    #  └── 30 another provider (9): not part of it
    table = {1: (0, 12), 20: (1, 1), 21: (20, 1), 22: (21, 4), 23: (21, 1), 24: (22, 2), 30: (1, 9)}

    assert tasks.tasks_below(table, 20) == 1 + 1 + 4 + 1 + 2
    assert tasks.tasks_below(table, 30) == 9
    assert tasks.tasks_below(table, 999) == 0, "a process that is gone holds nothing"


def test_the_real_count_follows_the_threads_of_a_real_process():
    started, release = threading.Event(), threading.Event()
    before = tasks.tasks_below(tasks.read_table(), os.getpid())

    def wait():
        started.set()
        release.wait(10)

    threads = [threading.Thread(target=wait) for _ in range(5)]
    for thread in threads:
        thread.start()
    try:
        started.wait(5)
        after = tasks.tasks_below(tasks.read_table(), os.getpid())
    finally:
        release.set()
        for thread in threads:
            thread.join()

    assert after - before == 5


# ------------------------------------------------------------------
# What the container allows
# ------------------------------------------------------------------


def test_the_pids_controller_of_cgroup_v2(tmp_path):
    (tmp_path / "pids.current").write_text("97\n")
    (tmp_path / "pids.max").write_text("2048\n")

    assert tasks.read_pids_cgroup((tmp_path,)) == tasks.PidsCgroup(97, 2048)


def test_no_pids_limit_is_max(tmp_path):
    (tmp_path / "pids.current").write_text("97\n")
    (tmp_path / "pids.max").write_text("max\n")

    assert tasks.read_pids_cgroup((tmp_path,)) == tasks.PidsCgroup(97, None)


def test_cgroup_v1_keeps_the_controller_in_a_directory_of_its_own(tmp_path):
    v2, v1 = tmp_path / "unified", tmp_path / "pids"
    v1.mkdir()
    (v1 / "pids.current").write_text("12\n")
    (v1 / "pids.max").write_text("512\n")

    assert tasks.read_pids_cgroup((v2, v1)) == tasks.PidsCgroup(12, 512)


def test_a_controller_that_cannot_be_read_is_not_a_limit(tmp_path):
    (tmp_path / "pids.current").write_text("not a number\n")
    (tmp_path / "pids.max").write_text("max\n")

    assert tasks.read_pids_cgroup((tmp_path,)) is None
    assert tasks.read_pids_cgroup((tmp_path / "missing",)) is None


# ------------------------------------------------------------------
# What a provider says when the kernel refuses it a thread
# ------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "RuntimeError: can't start new thread",
    "runtime: failed to create new OS thread (have 2 already; errno=11)",
    "pthread_create failed: Resource temporarily unavailable",
    "sh: fork: retry: Resource temporarily unavailable",
    "BlockingIOError: [Errno 11] Resource temporarily unavailable: could not fork a worker thread",
    "OSError: cannot fork: Resource temporarily unavailable",
])
def test_a_provider_that_cannot_create_a_task_is_recognised(line):
    assert tasks.is_exhaustion(line)


@pytest.mark.parametrize("line", [
    "BlockingIOError: [Errno 11] Resource temporarily unavailable",  # a non-blocking read, not a task
    "ConnectionResetError: [Errno 104] Connection reset by peer",
    "started a thread for the request",
    "",
])
def test_other_output_is_not(line):
    assert not tasks.is_exhaustion(line)


def test_the_numbers_say_what_is_held_and_what_counts():
    text = tasks.pressure_summary(tasks.PidsCgroup(2040, 2048), (512, 512), 1000, {"navidrome": (14, 512, 1024)})

    assert "container pids: 2040 of 2048 (pids_limit)" in text
    assert "RLIMIT_NPROC of the gateway: 512" in text and "uid 1000 on the whole host" in text
    assert "sandbox navidrome: 14 of 512 tasks (limits.processes), address space limited to 1024 MB" in text
    assert "\n" not in text


def test_no_pids_limit_and_an_unreadable_controller_are_both_said():
    unlimited = tasks.pressure_summary(tasks.PidsCgroup(90, None), (resource.RLIM_INFINITY, resource.RLIM_INFINITY), 10001)
    unknown = tasks.pressure_summary(None, (100, 100), 10001)

    assert "no limit set (pids_limit)" in unlimited and "RLIMIT_NPROC of the gateway: unlimited" in unlimited
    assert "container pids: unknown" in unknown


def test_by_default_the_numbers_are_read_from_this_process():
    text = tasks.pressure_summary()

    assert f"uid {os.getuid()}" in text and "RLIMIT_NPROC of the gateway: " in text


# ------------------------------------------------------------------
# The limit of a sandbox is the manifest's, counted by the gateway
# ------------------------------------------------------------------


def test_the_process_limit_of_the_manifest_is_still_capped_and_defaulted():
    ceilings = Ceilings(processes=100, default_processes=50)

    assert limits_for(Permissions(), ceilings).processes == 50
    assert limits_for(Permissions(limits=ResourceLimits(processes=8)), ceilings).processes == 8
    assert limits_for(Permissions(limits=ResourceLimits(processes=10**6)), ceilings).processes == 100


# ------------------------------------------------------------------
# The watchdog: a provider above its limit is stopped, a burst is not
# ------------------------------------------------------------------

THREADS_RUNNER = """
import os, socket, sys, threading, time
path = sys.argv[1]
server = socket.socket(socket.AF_UNIX)
server.bind(path)
server.listen(5)
open(path + ".pid", "w").write(str(os.getpid()))
for _ in range(int(sys.argv[2])):
    threading.Thread(target=time.sleep, args=(60,), daemon=True).start()
time.sleep(60)
"""


def declared_with(processes: int | None) -> DeclaredManifest:
    return DeclaredManifest("demo", ("lyrics",), PROVIDER_API_VERSION, Permissions(limits=ResourceLimits(processes=processes)))


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(sv, "TASK_CHECK_INTERVAL", 0.05)


def test_a_provider_that_holds_more_tasks_than_its_limit_is_stopped_and_the_reason_says_so(world, quick, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="musizerr_gateway.supervisor")
    monkeypatch.setattr(sv, "build_command", lambda spec, fd: [sys.executable, "-c", THREADS_RUNNER, str(spec.socket_dir / "provider.sock"), "30"])
    supervisor = world(Images(declared=declared_with(10)))

    supervisor.apply([ENTRY], {})

    managed = supervisor.get("demo")
    assert wait_for(lambda: managed.status.restarts >= 1), "it was stopped, and the supervisor restarts it with the usual pause"
    reason = managed.status.reason
    assert reason.startswith("process/thread limit reached: the sandbox held ") and "over its limit of 10" in reason
    assert "limits.processes" in reason
    assert managed.status.tasks.limit == 10 and managed.status.tasks.peak > 10
    assert any("process/thread limit reached" in record.getMessage() and "demo" in record.getMessage() for record in caplog.records)
    assert any("container pids" in record.getMessage() for record in caplog.records), "the log has the numbers"


def test_a_provider_within_its_limit_is_left_alone_and_its_usage_is_reported(world, quick, monkeypatch):
    monkeypatch.setattr(sv, "build_command", lambda spec, fd: [sys.executable, "-c", THREADS_RUNNER, str(spec.socket_dir / "provider.sock"), "5"])
    supervisor = world(Images(declared=declared_with(200)))

    supervisor.apply([ENTRY], {})

    managed = supervisor.get("demo")
    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING)
    assert wait_for(lambda: managed.status.tasks.current >= 6)
    assert managed.status.tasks.limit == 200 and 6 <= managed.status.tasks.peak <= 20
    assert managed.status.restarts == 0 and state_of(supervisor) == sv.RUNNING


def test_one_sample_over_the_limit_is_a_burst_not_a_leak(world, quick, monkeypatch):
    # What the gateway counts, sample after sample: over the limit of 10, under, over, under... never twice in a row.
    script = [40, 3] * 8
    monkeypatch.setattr(tasks, "tasks_below", lambda table, root: script.pop(0) if script else 3)
    supervisor = world(Images(declared=declared_with(10)))
    supervisor.apply([ENTRY], {})
    managed = supervisor.get("demo")

    assert wait_for(lambda: not script, 5), "all the samples were taken"
    assert managed.status.tasks.peak == 40
    assert managed.status.restarts == 0 and state_of(supervisor) == sv.RUNNING


def test_two_samples_in_a_row_over_the_limit_stop_it(world, quick, monkeypatch):
    script = [40, 3, 40, 41]
    monkeypatch.setattr(tasks, "tasks_below", lambda table, root: script.pop(0) if script else 3)
    supervisor = world(Images(declared=declared_with(10)))
    supervisor.apply([ENTRY], {})
    managed = supervisor.get("demo")

    assert wait_for(lambda: managed.status.restarts >= 1, 5)
    assert "held 41 processes and threads, over its limit of 10" in managed.status.reason


def test_a_provider_that_says_it_cannot_create_a_thread_is_logged_with_the_numbers(world, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="musizerr_gateway.supervisor")
    script = (
        "import os, socket, sys, time\n"
        "server = socket.socket(socket.AF_UNIX); server.bind(sys.argv[1]); server.listen(5)\n"
        "print(\"RuntimeError: can't start new thread\", flush=True)\n"
        "print(\"RuntimeError: can't start new thread\", flush=True)\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(sv, "build_command", lambda spec, fd: [sys.executable, "-c", script, str(spec.socket_dir / "provider.sock")])
    supervisor = world()

    supervisor.apply([ENTRY], {})

    managed = supervisor.get("demo")
    assert wait_for(lambda: managed.status.task_failures >= 2)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, "a provider repeats it on every attempt: the log says it once a minute"
    assert "process/thread limit reached (RLIMIT_NPROC / pids)" in errors[0]
    assert "can't start new thread" in errors[0] and "container pids" in errors[0] and "sandbox demo" in errors[0]
    assert managed.status.task_failure.startswith("process/thread limit reached (RLIMIT_NPROC / pids): ")
    view = provider_view(managed, world.config.run_dir)
    assert view["task_limit_hits"]["count"] >= 2 and "can't start new thread" in view["task_limit_hits"]["last"]
    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING), "reporting is not stopping"
    assert managed.status.restarts == 0


def test_the_listing_has_the_tasks_of_a_running_sandbox(world, quick, monkeypatch):
    monkeypatch.setattr(sv, "build_command", lambda spec, fd: [sys.executable, "-c", THREADS_RUNNER, str(spec.socket_dir / "provider.sock"), "3"])
    supervisor = world(Images(declared=declared_with(100)))
    supervisor.apply([ENTRY], {})
    managed = supervisor.get("demo")
    assert wait_for(lambda: managed.status.tasks is not None and managed.status.tasks.current >= 4)

    view = provider_view(managed, world.config.run_dir)

    assert view["tasks"]["limit"] == 100 and view["tasks"]["current"] >= 4 and view["tasks"]["peak"] >= view["tasks"]["current"]
    assert view["task_limit_hits"] == {"count": 0, "last": ""}


def test_the_ceiling_still_caps_what_a_manifest_asks_for_before_the_watchdog_counts_it(world):
    supervisor = world(Images(declared=declared_with(10**6)))

    supervisor.apply([ENTRY], {})

    assert wait_for(lambda: supervisor.get("demo").status.tasks is not None)
    assert supervisor.get("demo").status.tasks.limit == Ceilings().processes
