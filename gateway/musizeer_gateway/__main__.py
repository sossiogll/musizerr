"""``python -m musizeer_gateway``: run the gateway."""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from . import environment
from .config import ConfigError, from_environment
from .control import Control, ControlServer
from .images import ImageStore
from .supervisor import SandboxHealth, Supervisor
from .tasks import read_pids_cgroup

logger = logging.getLogger("musizeer_gateway")


def _report_task_limits(ceiling: int) -> None:
    """Say at start what bounds the processes and threads of the sandboxes, and warn when nothing does."""
    cgroup = read_pids_cgroup()
    if cgroup is None:
        logger.info("the container's pids controller cannot be read; each sandbox is still held to its limits.processes (ceiling %d)", ceiling)
    elif cgroup.limit is None:
        logger.warning(
            "the container has no pids limit (%d tasks now): set pids_limit on the gateway service so that a runaway provider "
            "cannot exhaust the host's process table; each sandbox is held to its limits.processes (ceiling %d) by sampling only",
            cgroup.current, ceiling,
        )
    else:
        logger.info("container pids limit: %d (%d now); each sandbox is held to its limits.processes (ceiling %d)", cgroup.limit, cgroup.current, ceiling)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    environment.report_at_start()
    try:
        config = from_environment()
    except ConfigError as exc:
        logger.error("configuration refused: %s", exc)
        return 2
    for directory in (config.state_dir, config.run_dir, config.providers_dir):
        directory.mkdir(parents=True, exist_ok=True)
    # The sockets are the way to call a provider with anyone's credentials: only the owner
    # may list the providers directory; the run directory itself is only crossed.
    os.chmod(config.providers_dir, 0o700)
    try:
        os.chmod(config.run_dir, 0o711)
    except PermissionError:
        pass

    _report_task_limits(config.ceilings.processes)
    health = SandboxHealth(config.sandbox_uid, config.sandbox_gid)
    health.check()
    supervisor = Supervisor(config, ImageStore(config), health)

    server = ControlServer(config.control_socket, Control(config, supervisor))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    # It runs nothing until the host pushes the desired set: the database of the application is the
    # only source of truth for what is installed, and the gateway keeps no list of its own.
    logger.info("gateway ready: control socket %s; waiting for the host to push the installed providers", config.control_socket)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        supervisor.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
