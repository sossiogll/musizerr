"""
TEST FIXTURE. A lyrics provider that starts threads on request and returns what happened as JSON in
``Lyrics.plain`` (the title of the track is the command):

- ``threads:<count>:<seconds>[:<stack KiB>]``  start ``count`` threads that live ``seconds`` seconds; reports how many started, the error
                                 that stopped it (``RuntimeError: can't start new thread``) and the threads it holds;
- ``info``                       the uid, the RLIMIT_NPROC the sandbox runs under, the threads it holds and an id of its
                                 process (a restart is a new id).

It declares a small ``limits.processes`` so that the gateway's count of a sandbox can be crossed.
"""
from __future__ import annotations

import json
import os
import resource
import sys
import threading
import time
import uuid

from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizeer_sdk.interfaces import PROVIDER_API_VERSION, LyricsProvider
from musizeer_sdk.models import Lyrics
from musizeer_sdk.permissions import Permissions, ResourceLimits

INSTANCE = uuid.uuid4().hex
LIMIT = 100


def held_threads() -> int:
    with open("/proc/self/status", encoding="utf-8") as status:
        for line in status:
            if line.startswith("Threads:"):
                return int(line.split()[1])
    return -1


def hold(seconds: float) -> None:
    """What a request handler does: allocate a little (which gives the thread a malloc arena of its own, unless glibc
    is held to fewer) and wait for the network."""
    data = [bytes(1000) for _ in range(50)]
    time.sleep(seconds)
    del data


def report(**extra) -> dict:
    return {
        "instance": INSTANCE, "uid": os.getuid(), "nproc": list(resource.getrlimit(resource.RLIMIT_NPROC)),
        "as": list(resource.getrlimit(resource.RLIMIT_AS)), "arena_max": os.environ.get("MALLOC_ARENA_MAX"),
        "threads": held_threads(), **extra,
    }


class TasksLyricsProvider(LyricsProvider):
    name = "tasks"
    label = "Tasks (test fixture)"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    provider_api_version = PROVIDER_API_VERSION
    # The default memory limit (1024 MB of address space) on purpose: a runner serves one thread per concurrent call, and
    # what a burst of them can reserve in it is part of what is tested.
    permissions = Permissions(limits=ResourceLimits(processes=LIMIT))

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session):
        return None

    def get_lyrics(self, track, session):
        command = track.title
        if command.startswith("threads:"):
            _, count, seconds, *stack = command.split(":")
            started, error = 0, None
            # The stack of the new threads: the default (8 MB, reserved in the sandbox's address space) unless a size in KiB is given.
            threading.stack_size(int(stack[0]) * 1024 if stack else 0)
            try:
                for _ in range(int(count)):
                    threading.Thread(target=hold, args=(float(seconds),), daemon=True).start()
                    started += 1
            except RuntimeError as exc:
                error = f"RuntimeError: {exc}"
                # What a runner that cannot start a thread of its own prints (a traceback ends in this line): the gateway reads the output.
                print(error, file=sys.stderr, flush=True)
            return Lyrics(plain=json.dumps(report(started=started, error=error)))
        return Lyrics(plain=json.dumps(report()))
