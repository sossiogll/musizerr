"""
Running the providers: one long-lived sandboxed process per desired provider.

Each provider has a thread that takes it from a desired entry to a running
process and keeps it there:

    pulling -> starting -> running -> (it dies) restarting -> starting -> ...

with a pause that doubles after every failure (and starts over once a process has
lived a while). Three states are final until something changes:

- ``refused``: the image is not what it was pinned to be (the reason says what);
- ``unavailable``: no sandbox can be made here. The provider does NOT run; unconfined
  is never a fallback. The sandbox is checked again after a while;
- ``failed``: it could not be started at all.

``generation`` changes whenever the set of RUNNING providers does, which is what the
host watches to rebuild its registries.

**Processes and threads.** The sandbox gets no ``RLIMIT_NPROC`` (the kernel counts it over the whole uid of the host,
``tasks.py``). The gateway counts the tasks of each sandbox every few seconds and stops one that stays above its
``limits.processes`` (it restarts with the usual pause and the reason says why); the container's pids limit is the
hard cap. A provider that reports it could not create a thread is logged with the numbers.

**Folders of the host** (``filesystem_from_settings``). The host grants the CURRENT values of the settings
a provider names together with its egress (``ManagedProvider.grant``); each is judged by ``filesystem.py`` and
bound into the sandbox at the same path. Mounts cannot change under a running process, so when the
binds a grant leads to differ from the ones the process was started with, the process is restarted
(no backoff, not counted as a crash) and the grant returns once the new one runs. Until a grant arrives a
provider runs WITHOUT the folder: its root is read-only, so a call that needs it fails cleanly and nothing
else of the host is reachable. (Not starting it until the grant came would deadlock: the host
only connects to, and so grants for, a provider that runs.) Every start judges the paths again, so a
link that was moved meanwhile is caught.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from musizerr_sdk.labels import DeclaredManifest

from . import tasks
from .config import GatewayConfig
from .egress import EgressPolicy, EgressServer
from .filesystem import Bind, PathRefused, make_mountpoint, validate_grant
from .images import ImageStore, PreparedImage
from .desired import DesiredProvider
from .oci import ImageError, RegistryError
from .sandbox import (
    EGRESS_SOCKET_NAME,
    SANDBOX_EGRESS_DIR,
    SANDBOX_SOCKET_DIR,
    SOCKET_NAME,
    SandboxSpec,
    SandboxUnavailable,
    build_command,
    explain_failure,
    limits_for,
    probe,
    seccomp_file,
)

logger = logging.getLogger(__name__)

PULLING, STARTING, RUNNING, RESTARTING = "pulling", "starting", "running", "restarting"
REFUSED, UNAVAILABLE, FAILED, STOPPED = "refused", "unavailable", "failed", "stopped"

READY_TIMEOUT = 30.0
BACKOFF_START, BACKOFF_MAX = 1.0, 60.0
STABLE_AFTER = 300.0
SANDBOX_RECHECK = 30.0
TAIL_LINES = 20
# The tasks (processes and threads) of a sandbox are counted this often; a provider above its limit (limits.processes)
# for this many samples in a row is stopped. One sample over is a burst, not a leak (tasks.py).
TASK_CHECK_INTERVAL = 2.0
TASK_STRIKES = 2
# A provider that cannot create a thread says so on every attempt: the gateway logs it at most this often.
EXHAUSTION_LOG_EVERY = 60.0
# How long a grant that restarts the provider waits for the new process (the host waits for the reply).
GRANT_RESTART_WAIT = 10.0


class SandboxHealth:
    """Whether a sandbox can be made here: checked once and again when asked or after a while."""

    def __init__(self, uid: int, gid: int, check: Callable[[int, int], None] = probe, recheck: float = SANDBOX_RECHECK) -> None:
        self._uid, self._gid, self._check, self._recheck = uid, gid, check, recheck
        self._lock = threading.Lock()
        self._at = 0.0
        self.available: bool | None = None
        self.reason = "not checked yet"

    def check(self, force: bool = False) -> bool:
        with self._lock:
            if force or self.available is None or (not self.available and time.monotonic() - self._at > self._recheck):
                try:
                    self._check(self._uid, self._gid)
                except SandboxUnavailable as exc:
                    self.available, self.reason = False, str(exc)
                    logger.error("sandbox unavailable: %s", exc)
                else:
                    self.available, self.reason = True, "ok"
                self._at = time.monotonic()
            return bool(self.available)


@dataclass
class TaskUsage:
    """The tasks (processes and threads) a sandbox holds, as the gateway last counted them, against its limit."""

    limit: int
    memory_mb: int
    current: int = 0
    peak: int = 0


@dataclass
class ProviderStatus:
    entry: DesiredProvider
    state: str = PULLING
    reason: str = ""
    restarts: int = 0
    started_at: float | None = None
    declared: DeclaredManifest | None = None
    policy: EgressPolicy | None = None
    # The tasks of the running process against ``limits.processes``, and how often (and with what words) the
    # provider reported that the kernel would not create one.
    tasks: TaskUsage | None = None
    task_failures: int = 0
    task_failure: str = ""
    # The folders bound into the running process, and why a granted path is not (setting -> reason).
    binds: tuple[Bind, ...] = ()
    path_refusals: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GrantReport:
    targets: int  # destinations understood
    binds: tuple[Bind, ...]  # the folders bound into the process that serves now
    refused: dict[str, str]  # setting -> why its folder is not granted
    restarted: bool  # the process was restarted to change its mounts


class ManagedProvider:
    """One desired provider and the thread that keeps it running."""

    def __init__(
        self, entry: DesiredProvider, config: GatewayConfig, images: ImageStore, health: SandboxHealth,
        on_change: Callable[[], None], credential: str | None = None,
    ) -> None:
        self.status = ProviderStatus(entry)
        # Held in memory for the pull only: dropped as soon as the image is unpacked.
        self._credential = credential
        self._credential_lock = threading.Lock()
        self._config = config
        self._images = images
        self._health = health
        self._on_change = on_change
        self._pulled = False
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        # The current values of the settings that name folders (setting -> path), as the host last granted them.
        self._paths: dict[str, str] = {}
        self._rootfs: Path | None = None  # the image's root filesystem: where the mount points are made
        self._bind_lock = threading.Lock()
        self._running_binds: tuple[Bind, ...] | None = None  # the binds of the process started last; None: none
        self._rebind = threading.Event()
        self._starts = 0
        self._task_stop = ""  # why the process was stopped for holding too many tasks; empty: it was not
        self._exhaustion_logged = float("-inf")
        self._tail: deque[str] = deque(maxlen=TAIL_LINES)
        self._thread = threading.Thread(target=self._run, name=f"provider-{entry.name}", daemon=True)

    @property
    def name(self) -> str:
        return self.status.entry.name

    @property
    def socket_path(self) -> Path:
        return self._config.providers_dir / self.name / SOCKET_NAME

    def start(self) -> None:
        self._thread.start()

    def offer_credential(self, credential: str | None) -> None:
        """The host sent the set again: remember the credential if the image still has to be pulled."""
        with self._credential_lock:
            if self._pulled:
                return
            self._credential = credential

    def _take_credential(self) -> str | None:
        with self._credential_lock:
            return self._credential

    def _forget_credential(self) -> None:
        with self._credential_lock:
            self._credential, self._pulled = None, True

    def stop(self, wait: float = 10.0) -> None:
        self._stop.set()
        self._terminate()
        self._thread.join(wait)
        self._set(STOPPED, "")

    # ---- state ----------------------------------------------------

    def _set(self, state: str, reason: str = "") -> None:
        changed = (state == RUNNING) != (self.status.state == RUNNING)
        self.status.state, self.status.reason = state, reason
        if changed:
            self._on_change()

    def _pause(self, seconds: float) -> bool:
        """Wait; True when the provider has been stopped meanwhile."""
        return self._stop.wait(seconds)

    # ---- the life of the process ----------------------------------

    def _run(self) -> None:
        entry = self.status.entry
        backoff = BACKOFF_START
        prepared: PreparedImage | None = None
        while not self._stop.is_set():
            if prepared is None:
                self._set(PULLING, "")
                try:
                    prepared = self._images.prepare(entry, self._take_credential())
                except ImageError as exc:
                    logger.error("%s: image refused: %s", entry.name, exc)
                    self._set(REFUSED, str(exc))
                    return
                except RegistryError as exc:
                    logger.warning("%s: the registry could not be reached: %s", entry.name, exc)
                    self._set(FAILED, f"registry: {exc}")
                    if self._pause(backoff):
                        return
                    backoff = min(backoff * 2, BACKOFF_MAX)
                    continue
                except Exception as exc:  # noqa: BLE001 — a supervisor thread must not die on a surprise
                    logger.exception("%s: pulling failed", entry.name)
                    self._set(FAILED, f"pull: {exc}")
                    if self._pause(backoff):
                        return
                    backoff = min(backoff * 2, BACKOFF_MAX)
                    continue
                self._forget_credential()
                self.status.declared = prepared.declared
                self._rootfs = prepared.rootfs
            if not self._health.check():
                self._set(UNAVAILABLE, f"sandbox unavailable: {self._health.reason}")
                if self._pause(SANDBOX_RECHECK):
                    return
                continue
            started = time.monotonic()
            try:
                code = self._run_once(prepared)
            except SandboxUnavailable as exc:
                self._health.check(force=True)
                self._set(UNAVAILABLE, f"sandbox unavailable: {exc}")
                if self._pause(SANDBOX_RECHECK):
                    return
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("%s: could not be started", entry.name)
                code, detail = None, f"could not be started: {exc}"
            else:
                detail = f"exited with status {code}" + (f": {self._tail[-1]}" if self._tail else "")
                if self._task_stop:
                    detail = f"{self._task_stop} (exited with status {code})"
            if self._stop.is_set():
                return
            if self._rebind.is_set():
                # The mounts a grant leads to are not the ones this process had: start again with the new
                # ones, at once. Not a crash.
                self._rebind.clear()
                self._set(RESTARTING, "the folders granted to it changed")
                logger.info("%s: restarting with the folders its settings now name", entry.name)
                continue
            if time.monotonic() - started > STABLE_AFTER:
                backoff = BACKOFF_START
            self.status.restarts += 1
            self._set(RESTARTING, detail)
            logger.warning("%s: %s; restarting in %.0f s", entry.name, detail, backoff)
            if self._pause(backoff):
                return
            backoff = min(backoff * 2, BACKOFF_MAX)

    def _run_once(self, prepared: PreparedImage) -> int | None:
        entry, declared = prepared.entry, prepared.declared
        config = self._config
        socket_dir = config.providers_dir / entry.name
        egress_dir = config.egress_dir / entry.name
        workspace = config.workspace_dir / entry.name
        socket_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(socket_dir, 0o700)
        self.socket_path.unlink(missing_ok=True)
        egress_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not workspace.exists():
            workspace.mkdir(parents=True, mode=0o711)
            os.chmod(workspace, 0o711)

        policy = self.status.policy or EgressPolicy(
            entry.name, declared.permissions.egress, tcp=declared.permissions.tcp_from_settings,
        )
        self.status.policy = policy
        self._set(STARTING, "")
        with self._bind_lock:
            self._rebind.clear()  # this start uses what is granted now
            binds, refused = self._resolve_paths(self._paths, prepared.rootfs)
            self._running_binds = binds
            self.status.binds, self.status.path_refusals = binds, refused
        for setting, reason in refused.items():
            logger.warning("%s: the folder of '%s' is not granted: %s", entry.name, setting, reason)
        egress = EgressServer(egress_dir / EGRESS_SOCKET_NAME, policy)
        threading.Thread(target=egress.serve_forever, name=f"egress-{entry.name}", daemon=True).start()
        spec = SandboxSpec(
            rootfs=prepared.rootfs, socket_dir=socket_dir, egress_dir=egress_dir, workspace_dir=workspace,
            command=(
                *prepared.command,
                "--socket", f"{SANDBOX_SOCKET_DIR}/{SOCKET_NAME}",
                "--egress-socket", f"{SANDBOX_EGRESS_DIR}/{EGRESS_SOCKET_NAME}",
            ),
            limits=limits_for(declared.permissions, config.ceilings),
            uid=config.sandbox_uid, gid=config.sandbox_gid, tmp_mb=config.ceilings.tmp_mb,
            image_env=prepared.config.env,
            binds=binds,
        )
        self._task_stop = ""
        self.status.tasks = TaskUsage(spec.limits.processes, spec.limits.memory_mb)
        try:
            with seccomp_file() as fd:
                process = subprocess.Popen(
                    build_command(spec, fd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    pass_fds=(fd,), text=True, errors="replace",
                    # bubblewrap's own environment stays readable from inside (/proc/1/environ is the
                    # sandbox's init): it gets none of the gateway's, and the provider's comes from `--setenv`.
                    env={},
                )
            self._process = process
            if self._rebind.is_set():  # a grant came between the binds and the process
                self._terminate()
            self._tail.clear()
            reader = threading.Thread(target=self._log_output, args=(process,), name=f"log-{entry.name}", daemon=True)
            reader.start()
            threading.Thread(target=self._watch_tasks, args=(process, spec.limits.processes), name=f"tasks-{entry.name}", daemon=True).start()
            if not self._wait_ready(process):
                if process.poll() is None:
                    self._terminate()
                code = process.wait()
                reader.join(2)
                self._check_sandbox_failure()
                return code
            self.status.started_at = time.time()
            self._starts += 1
            self._set(RUNNING, "")
            logger.info("%s: running (pid %d)", entry.name, process.pid)
            return process.wait()
        finally:
            self._process = None
            with self._bind_lock:
                self._running_binds = None
                self.status.binds = ()
            egress.shutdown()
            egress.server_close()
            self.socket_path.unlink(missing_ok=True)

    # ---- what the host grants -------------------------------------

    def _resolve_paths(self, values: Mapping[str, str], rootfs: Path | None = None) -> tuple[tuple[Bind, ...], dict[str, str]]:
        """``(binds, refusals)`` for the folders the settings hold now. The mode of each is the one the
        VERIFIED image declares for that setting. With ``rootfs`` the mount points are made in the image's
        root filesystem too (a failure there refuses that folder)."""
        declared = self.status.declared
        modes = {a.setting: a.mode for a in declared.permissions.filesystem_from_settings} if declared else {}
        config = self._config
        binds: dict[str, Bind] = {}
        owners: dict[str, str] = {}
        refused: dict[str, str] = {}
        for setting in sorted(values):
            mode = modes.get(setting)
            if mode is None:
                refused[setting] = "not a setting this provider declared in filesystem_from_settings"
                continue
            try:
                bind = validate_grant(
                    values[setting], mode, config.filesystem_roots, (config.state_dir, config.run_dir), str(config.workspace_dir),
                )
                if bind.dest in binds:
                    raise PathRefused(f"already granted by the setting '{owners[bind.dest]}'")
                if rootfs is not None:
                    make_mountpoint(rootfs, bind.dest)
            except PathRefused as exc:
                refused[setting] = str(exc)
                logger.debug("%s: the folder of '%s' is not granted: %s", self.name, setting, exc)
                continue
            binds[bind.dest] = bind
            owners[bind.dest] = setting
        return tuple(binds[dest] for dest in sorted(binds)), refused

    def grant(
        self, targets: list[str], ttl: float, tcp: Mapping[str, list[str]] | None = None,
        paths: Mapping[str, str] | None = None, wait: float = GRANT_RESTART_WAIT,
    ) -> "GrantReport":
        """The host tells what the provider's settings name NOW (it alone knows): the destinations
        (``egress_from_settings``), the hosts for raw TCP (``tcp_from_settings``) and the folders
        (``filesystem_from_settings``). A grant REPLACES the previous one. The destinations expire after
        ``ttl``; the folders do not (they are mounts, and restarting a process because a clock ran out would
        cut calls in flight): they stay until another grant replaces them. A change in the folders that
        are bound restarts the process, and this returns once the new one runs (or after ``wait``)."""
        policy = self.status.policy
        assert policy is not None  # the caller (control) checked it
        count = policy.grant(targets, ttl, tcp)
        wanted, refused = self._resolve_paths(paths or {}, self._rootfs)
        with self._bind_lock:
            self._paths = dict(paths or {})
            self.status.path_refusals = refused
            running = self._running_binds
            starts = self._starts
            restart = running is not None and running != wanted
            if restart:
                self._rebind.set()
        if restart:
            logger.info("%s: the folders granted to it changed; restarting it", self.name)
            self._terminate()
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and not self._stop.is_set():
                if self._starts > starts and self.status.state == RUNNING:
                    break
                time.sleep(0.05)
        return GrantReport(count, tuple(self.status.binds), dict(self.status.path_refusals), restart)

    def _check_sandbox_failure(self) -> None:
        """A process that dies at once may have died because the sandbox could not be made."""
        lines = list(self._tail)
        start = next((index for index, line in enumerate(lines) if "bwrap:" in line.lower()), None)
        if start is not None:
            raise SandboxUnavailable(explain_failure("\n".join(lines[start:])) or "bubblewrap failed")

    def _wait_ready(self, process: subprocess.Popen) -> bool:
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline and not self._stop.is_set():
            if process.poll() is not None:
                return False
            if self.socket_path.exists():
                probe_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    probe_socket.settimeout(1)
                    probe_socket.connect(str(self.socket_path))
                    return True
                except OSError:
                    pass
                finally:
                    probe_socket.close()
            time.sleep(0.1)
        return False

    def _log_output(self, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if line:
                self._tail.append(line)
                logger.info("[%s] %s", self.name, line)
                if tasks.is_exhaustion(line):
                    self._note_exhaustion(line)

    # ---- processes and threads ------------------------------------

    def _watch_tasks(self, process: subprocess.Popen, limit: int) -> None:
        """Count the tasks (processes and threads) of the sandbox every few seconds; stop the provider
        when it stays above ``limits.processes``. The kernel's RLIMIT_NPROC cannot do this per sandbox
        (it counts the whole uid of the host, ``tasks.py``); the container's pids limit is what stops a burst
        between two samples."""
        usage = self.status.tasks
        assert usage is not None
        strikes = 0
        while not self._stop.wait(TASK_CHECK_INTERVAL):
            if process.poll() is not None:
                return
            held = tasks.tasks_below(tasks.read_table(), process.pid)
            usage.current, usage.peak = held, max(usage.peak, held)
            strikes = strikes + 1 if held > limit else 0
            if strikes < TASK_STRIKES:
                continue
            self._task_stop = (
                f"process/thread limit reached: the sandbox held {held} processes and threads, over its limit of {limit} "
                f"(limits.processes of its manifest, ceiling GATEWAY_MAX_PROCESSES)"
            )
            summary = tasks.pressure_summary(sandboxes={self.name: (held, limit, usage.memory_mb)})
            logger.error("%s: %s; stopping it. %s", self.name, self._task_stop, summary)
            self._terminate()
            return

    def _note_exhaustion(self, line: str) -> None:
        """The provider says the kernel would not create a thread or a process: record it, and say what that
        means with the numbers (at most once a minute in the log: a provider repeats it on every attempt)."""
        status = self.status
        status.task_failures += 1
        usage = status.tasks
        sandboxes = {self.name: (usage.current, usage.limit, usage.memory_mb)} if usage else None
        status.task_failure = f"process/thread limit reached (RLIMIT_NPROC / pids): {line}"
        now = time.monotonic()
        if now - self._exhaustion_logged < EXHAUSTION_LOG_EVERY:
            return
        self._exhaustion_logged = now
        logger.error(
            "%s: process/thread limit reached (RLIMIT_NPROC / pids): the provider reported \"%s\" (%d so far). %s [%s]",
            self.name, line, status.task_failures, tasks.TASK_LIMIT_HINT, tasks.pressure_summary(sandboxes=sandboxes),
        )

    def _terminate(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()


# ------------------------------------------------------------------


class Supervisor:
    """Every desired provider, and the set of them kept in line with what the host pushed."""

    def __init__(self, config: GatewayConfig, images: ImageStore, health: SandboxHealth) -> None:
        self._config = config
        self._images = images
        self.health = health
        self._providers: dict[str, ManagedProvider] = {}
        self._lock = threading.RLock()
        self.rejected: dict[str, str] = {}
        self.desired_generation: str | None = None
        self.epoch = uuid.uuid4().hex
        self._generation = 0
        self._generation_lock = threading.Lock()

    @property
    def generation(self) -> int:
        return self._generation

    def _changed(self) -> None:
        # Its own lock: providers report from their threads while `apply` holds `_lock` waiting for them.
        with self._generation_lock:
            self._generation += 1

    def apply(
        self, entries: list[DesiredProvider], rejected: dict[str, str], credentials: dict[str, str] | None = None,
        generation: str | None = None,
    ) -> None:
        """Make the running set match ``entries``: start the new, stop the removed, restart
        the ones whose image changed. ``credentials`` (by name) are for pulling and go no further."""
        credentials = credentials or {}
        with self._lock:
            self.rejected = dict(rejected)
            self.desired_generation = generation
            wanted = {entry.name: entry for entry in entries}
            for name in list(self._providers):
                current = self._providers[name].status.entry
                # A refused image gets another try whenever the host pushes the set again (its
                # credential or the registry may have been fixed); the other states retry by themselves.
                if name not in wanted or wanted[name] != current or self._providers[name].status.state == REFUSED:
                    self._providers.pop(name).stop()
                    self._changed()
            for name, entry in wanted.items():
                if name in self._providers:
                    self._providers[name].offer_credential(credentials.get(name))
                else:
                    managed = ManagedProvider(entry, self._config, self._images, self.health, self._changed, credentials.get(name))
                    self._providers[name] = managed
                    managed.start()
                    self._changed()

    def providers(self) -> list[ManagedProvider]:
        with self._lock:
            return list(self._providers.values())

    def get(self, name: str) -> ManagedProvider | None:
        with self._lock:
            return self._providers.get(name)

    def stop(self) -> None:
        with self._lock:
            providers = list(self._providers.values())
            self._providers.clear()
        for managed in providers:
            managed.stop()
