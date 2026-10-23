"""
TEST FIXTURE. A lyrics provider that starts threads on request and returns what happened as JSON in
``Lyrics.plain`` (the title of the track is the command):

- ``threads:<count>:<seconds>``  start ``count`` threads that live ``seconds`` seconds; reports how many started, the error
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

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.interfaces import PROVIDER_API_VERSION, LyricsProvider
from musizerr_sdk.models import Lyrics
from musizerr_sdk.permissions import Permissions, ResourceLimits

INSTANCE = uuid.uuid4().hex
LIMIT = 64


def held_threads() -> int:
    with open("/proc/self/status", encoding="utf-8") as status:
        for line in status:
            if line.startswith("Threads:"):
                return int(line.split()[1])
    return -1


def report(**extra) -> dict:
    return {
        "instance": INSTANCE, "uid": os.getuid(), "nproc": list(resource.getrlimit(resource.RLIMIT_NPROC)),
        "threads": held_threads(), **extra,
    }


class TasksLyricsProvider(LyricsProvider):
    name = "tasks"
    label = "Tasks (test fixture)"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    provider_api_version = PROVIDER_API_VERSION
    # 2048 MB of address space (the gateway's ceiling): a Python thread reserves about 11 MB of it here, so the default 1024 MB would stop
    # the threads of the watchdog check before the number of tasks does.
    permissions = Permissions(limits=ResourceLimits(processes=LIMIT, memory_mb=2048))

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session):
        return None

    def get_lyrics(self, track, session):
        command = track.title
        if command.startswith("threads:"):
            _, count, seconds = command.split(":")
            started, error = 0, None
            # Small stacks: each thread reserves its stack in the sandbox's address space (limits.memory_mb), and this
            # test is about the NUMBER of tasks, not about the memory they reserve.
            threading.stack_size(128 * 1024)
            try:
                for _ in range(int(count)):
                    threading.Thread(target=time.sleep, args=(float(seconds),), daemon=True).start()
                    started += 1
            except RuntimeError as exc:
                error = f"RuntimeError: {exc}"
                # What a runner that cannot start a thread of its own prints (a traceback ends in this line): the gateway reads the output.
                print(error, file=sys.stderr, flush=True)
            return Lyrics(plain=json.dumps(report(started=started, error=error)))
        return Lyrics(plain=json.dumps(report()))
