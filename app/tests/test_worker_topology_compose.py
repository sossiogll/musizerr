"""
The compose files run the topology of docs/workers.md: every queue of
``workers.celery_app.QUEUES`` consumed by exactly one worker service, and one
beat. A queue nobody consumes never runs its tasks (after the split, a worker
left on the old ``--queues=downloads,default`` runs no routine and no job);
two beats send every tick twice.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from workers.celery_app import QUEUE_DOWNLOADS, QUEUES

_REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILES = ["docker-compose.yaml", "docker-compose.dev.yml"]


def _commands(path: Path) -> dict[str, str]:
    services = yaml.safe_load(path.read_text())["services"]
    commands = {}
    for name, service in services.items():
        command = service.get("command")
        if isinstance(command, list):
            command = " ".join(command)
        if command and "workers.celery_app" in command:
            commands[name] = " ".join(command.split())
    return commands


@pytest.mark.parametrize("compose", COMPOSE_FILES)
def test_every_queue_has_exactly_one_worker_and_there_is_one_beat(compose):
    path = _REPO / compose
    if not path.exists():
        pytest.skip(f"{compose} not in this checkout")
    commands = _commands(path)

    consumers: dict[str, list[str]] = {queue: [] for queue in QUEUES}
    beats = []
    for name, command in commands.items():
        tokens = command.split()
        if "worker" in tokens:
            match = re.search(r"--queues[= ](\S+)", command)
            assert match, f"{name} consumes no explicit queue"
            for queue in match.group(1).split(","):
                consumers.setdefault(queue, []).append(name)
            if "-B" in tokens or "--beat" in tokens:
                beats.append(name)
        if "beat" in tokens:
            beats.append(name)

    for queue in QUEUES:
        assert len(consumers[queue]) == 1, (compose, queue, consumers[queue])
    assert len(beats) == 1, (compose, beats)
    # The downloads worker serves the downloads only: a slow download never
    # holds a routine or a user's job.
    downloads_worker = consumers[QUEUE_DOWNLOADS][0]
    assert [q for q, names in consumers.items() if downloads_worker in names] == [QUEUE_DOWNLOADS]
