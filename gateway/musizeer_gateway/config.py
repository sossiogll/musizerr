"""The gateway's settings: environment variables, read once at start (through ``environment.py``, its registry)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import environment


class ConfigError(ValueError):
    """A setting the gateway refuses to start with."""


@dataclass(frozen=True)
class Ceilings:
    """The most any provider may be given, whatever its manifest asks, and what it
    gets when it asks for nothing. ``processes`` counts tasks (processes and threads) of ONE sandbox, as the
    gateway counts them (``tasks.py``): it is not an ``RLIMIT_NPROC``, which the kernel counts for the whole uid."""

    memory_mb: int = 2048
    open_files: int = 4096
    processes: int = 1024
    default_memory_mb: int = 1024
    default_open_files: int = 1024
    default_processes: int = 512
    tmp_mb: int = 256


@dataclass(frozen=True)
class GatewayConfig:
    # Everything the gateway keeps: layer cache, unpacked images, egress sockets.
    state_dir: Path = Path("/var/lib/musizeer-gateway")
    # Shared with the application and the workers: the control socket and, under
    # ``providers/<name>/``, the socket of each provider.
    run_dir: Path = Path("/run/musizeer")
    # The uid/gid the provider sees itself as inside its sandbox (the only one).
    sandbox_uid: int = 10001
    sandbox_gid: int = 10001
    ceilings: Ceilings = Ceilings()
    # There is NO folder here (ADR-0008): the root folders and the working folder are declared by the host
    # (``{"op": "install"}``, ``control.py``) and judged by ``roots.py``; the mounts are the bound.

    @property
    def control_socket(self) -> Path:
        return self.run_dir / "gateway.sock"

    @property
    def providers_dir(self) -> Path:
        return self.run_dir / "providers"

    @property
    def layers_dir(self) -> Path:
        return self.state_dir / "layers"

    @property
    def images_dir(self) -> Path:
        return self.state_dir / "images"

    @property
    def egress_dir(self) -> Path:
        return self.state_dir / "egress"


def from_environment(environ: Mapping[str, str] | None = None) -> GatewayConfig:
    """The configuration of ``environ`` (the process environment when none is given); the defaults are the registry's."""
    def text(name: str) -> str:
        return environment.get(name, environ)

    def number(name: str) -> int:
        try:
            return environment.get_int(name, environ)
        except ValueError as refused:
            raise ConfigError(str(refused)) from None

    ceilings = Ceilings(
        memory_mb=number("MUSIZEER_GATEWAY_MAX_MEMORY_MB"),
        open_files=number("MUSIZEER_GATEWAY_MAX_OPEN_FILES"),
        processes=number("MUSIZEER_GATEWAY_MAX_PROCESSES"),
    )
    return GatewayConfig(
        state_dir=Path(text("MUSIZEER_GATEWAY_STATE_DIR")),
        run_dir=Path(text("MUSIZEER_GATEWAY_RUN_DIR")),
        sandbox_uid=number("MUSIZEER_GATEWAY_SANDBOX_UID"),
        sandbox_gid=number("MUSIZEER_GATEWAY_SANDBOX_GID"),
        ceilings=ceilings,
    )

