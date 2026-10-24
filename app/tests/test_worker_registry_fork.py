"""
The registry of a Celery worker lives in the processes that run tasks, not in the parent.

A prefork worker runs its tasks in children forked from a parent. A registry the parent built
(it is built before the gateway has started its providers, so usually empty) would reach every
child as a frozen copy, and the thread that rebuilds it on the providers-changed event does not
survive the fork. ``workers.celery_app.get_registry`` therefore builds one per process, the
first time the process asks, with its own reloader (``services/process_registry.py``).

These tests FORK for real (the pool does): a child is a process with one thread, the way a
worker child is. Every wait has a deadline, a child that does not answer is killed.
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import provider_events
from services.gateway_client import GatewayProviderContext
from services.process_registry import ProcessRegistry
from workers import celery_app

from tests.gateway_support import FakeGateway, Remote, gateway  # noqa: F401 — `gateway` is a fixture

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="a prefork pool needs fork")

DEADLINE = 20.0
APP_ROOT = Path(celery_app.__file__).resolve().parent.parent


def named(name: str):
    """A lyrics provider class of that name."""
    return type(name.title(), (Remote,), {"name": name})


class EventPipe:
    """A providers-changed subscription fed by a pipe: what the parent writes, the child hears."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def get_message(self, timeout=0):
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return None
        os.read(self._fd, 1)
        return {"data": "changed"}

    def close(self) -> None:
        pass


class Idle:
    def get_message(self, timeout=0):
        time.sleep(min(timeout, 0.05))
        return None

    def close(self) -> None:
        pass


class Child:
    """A forked process running ``target(say)``; ``say(value)`` sends the parent a JSON line."""

    def __init__(self, target) -> None:
        self._read, write = os.pipe()
        self._buffer = b""
        self.pid = os.fork()
        if self.pid == 0:  # the child: never returns, never runs the test runner's teardown
            code = 0
            try:
                os.close(self._read)

                def say(value) -> None:
                    os.write(write, json.dumps(value).encode() + b"\n")

                target(say)
            except BaseException as exc:  # noqa: BLE001
                os.write(write, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode() + b"\n")
                code = 1
            finally:
                os._exit(code)
        os.close(write)

    def said(self, timeout: float = DEADLINE):
        """The next thing the child said; fails the test when it says nothing in time or failed."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self._read], [], [], remaining)[0]:
                self.kill()
                pytest.fail("the child said nothing in time")
            chunk = os.read(self._read, 65536)
            if not chunk:
                self.kill()
                pytest.fail("the child ended without saying it")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        value = json.loads(line)
        if isinstance(value, dict) and "error" in value:
            pytest.fail(f"the child failed: {value['error']}")
        return value

    def finish(self, timeout: float = DEADLINE) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                os.close(self._read)
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.01)
        self.kill()
        pytest.fail("the child did not end in time")

    def kill(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass


def wait_until(condition, timeout: float = DEADLINE) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def worker(gateway, monkeypatch):
    """The worker module, pointed at the fake gateway, with a fresh registry holder and a
    providers-changed subscription that is idle in this (the parent) process and, in a child,
    a pipe the test writes to."""
    parent = os.getpid()
    read, write = os.pipe()
    monkeypatch.setenv("MUSIZEER_GATEWAY_SOCKET", str(gateway.run_dir / "gateway.sock"))
    monkeypatch.setattr(GatewayProviderContext, "settings", lambda self: {})
    monkeypatch.setattr(celery_app, "_process_registry", ProcessRegistry())
    monkeypatch.setattr(
        provider_events.ProviderReloader, "_redis_pubsub",
        staticmethod(lambda: Idle() if os.getpid() == parent else EventPipe(read)),
    )
    yield SimpleNamespace(announce=lambda: os.write(write, b"c"), holder=celery_app._process_registry)
    reloader = celery_app._process_registry.reloader
    if reloader is not None:
        reloader.stop()
    os.close(read)
    os.close(write)


def test_a_child_forked_after_an_empty_registry_sees_the_providers_and_follows_the_gateway(gateway, worker):
    # The worker starts before the gateway runs anything: what the parent's registry holds is empty.
    assert celery_app.get_registry().lyrics_names() == []
    gateway.serve(named("first"), "first")
    gateway.generation = 2

    def task_child(say) -> None:
        registry = celery_app.get_registry()
        say({"at_first_use": registry.lyrics_names()})
        assert wait_until(lambda: registry.lyrics_names() == ["first", "second"]), registry.lyrics_names()
        say({"after_the_event": registry.lyrics_names()})

    child = Child(task_child)
    assert child.said() == {"at_first_use": ["first"]}, "the child builds its own, it does not inherit the empty one"

    gateway.serve(named("second"), "second")  # a provider comes up while the child lives
    gateway.generation = 3
    worker.announce()  # the API's watcher notices and announces it

    assert child.said() == {"after_the_event": ["first", "second"]}
    assert child.finish() == 0


def test_a_child_that_started_before_the_gateway_had_providers_fills_itself_when_they_appear(gateway, worker):
    def task_child(say) -> None:
        registry = celery_app.get_registry()
        say({"at_first_use": registry.lyrics_names()})
        assert wait_until(lambda: registry.lyrics_names() == ["late"]), registry.lyrics_names()
        say({"after_the_event": registry.lyrics_names()})

    child = Child(task_child)
    assert child.said() == {"at_first_use": []}
    gateway.serve(named("late"), "late")
    gateway.generation = 2
    worker.announce()

    assert child.said() == {"after_the_event": ["late"]}
    assert child.finish() == 0


def test_every_child_has_its_own_reloader_and_the_parents_is_not_theirs(gateway, worker):
    parent_registry = celery_app.get_registry()
    parent_reloader = worker.holder.reloader
    assert parent_reloader is not None and parent_reloader._thread.is_alive()  # noqa: SLF001

    def task_child(say) -> None:
        say({"inherited_is_hidden": worker.holder.reloader is None})
        registry = celery_app.get_registry()
        reloader = worker.holder.reloader
        say({
            "own_registry": registry is not parent_registry,
            "own_reloader": reloader is not None and reloader is not parent_reloader,
            "listening": reloader._thread.is_alive(),  # noqa: SLF001
            "same_registry_again": celery_app.get_registry() is registry,
            "pid": os.getpid(),
        })

    reports = []
    for _ in range(2):
        child = Child(task_child)
        assert child.said() == {"inherited_is_hidden": True}
        reports.append(child.said())
        assert child.finish() == 0

    for report in reports:
        assert report["own_registry"] and report["own_reloader"] and report["listening"] and report["same_registry_again"]
    assert reports[0]["pid"] != reports[1]["pid"] != os.getpid()
    assert worker.holder.reloader is parent_reloader, "the parent's own reloader is untouched"


def test_threads_of_one_process_share_one_registry_and_one_reloader(gateway, worker):
    seen, errors = [], []

    def ask() -> None:
        try:
            seen.append(celery_app.get_registry())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(DEADLINE)

    assert not errors and len(seen) == 8 and all(registry is seen[0] for registry in seen)
    assert sum(1 for t in threading.enumerate() if t.name == "provider-reloader" and t.is_alive()) == 1


def test_the_parent_of_a_worker_builds_no_registry_and_starts_no_reloader(worker, monkeypatch):
    """``worker_init`` runs in the parent (and in the maintenance worker started with ``-B``, so does
    its beat): what a task needs is built by the processes that run tasks."""
    monkeypatch.setattr(celery_app, "_require_schema", lambda: None)  # the start check has its own tests
    monkeypatch.setattr(celery_app, "_reap_orphaned_running_downloads", lambda: 0)
    monkeypatch.setattr(celery_app, "_reap_orphaned_library_jobs", lambda: 0)
    monkeypatch.setattr(celery_app, "_config", None)  # restored afterwards: the signal handler sets both
    monkeypatch.setattr(celery_app, "_consumed", frozenset())
    queues = SimpleNamespace(consume_from={"downloads": object(), "jobs": object()})
    sender = SimpleNamespace(app=SimpleNamespace(amqp=SimpleNamespace(queues=queues)))

    celery_app._on_worker_init(sender=sender)  # noqa: SLF001

    assert worker.holder.reloader is None
    assert not [t for t in threading.enumerate() if t.name == "provider-reloader" and t.is_alive()]
    assert celery_app._consumed == frozenset({"downloads", "jobs"})  # noqa: SLF001


def test_beat_imports_the_worker_module_without_a_registry_or_a_listener(tmp_path):
    """A standalone ``celery beat`` imports ``workers.celery_app`` and only sends tasks."""
    from tests.credentials_support import KEY

    env = {k: v for k, v in os.environ.items() if k not in ("MUSIZEER_CREDENTIALS_KEY", "MUSIZEER_CREDENTIALS_KEY_PREVIOUS", "DATABASE_URL")}
    env.update(MUSIZEER_DATA_DIR=str(tmp_path), PYTHONDONTWRITEBYTECODE="1", REDIS_URL="redis://localhost:6379/0", MUSIZEER_CREDENTIALS_KEY=KEY)
    script = (
        "import threading, workers.celery_app as c;"
        "print(c._process_registry.reloader is None,"
        " [t.name for t in threading.enumerate() if t.name == 'provider-reloader'],"
        " 'workers.routines.tick' in [e['task'] for e in c.celery.conf.beat_schedule.values()])"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=APP_ROOT, env=env, capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True [] True"
