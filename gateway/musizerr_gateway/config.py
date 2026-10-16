"""The gateway's settings: environment variables, read once at start."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .filesystem import PathRefused, check_root, inside


class ConfigError(ValueError):
    """A setting the gateway refuses to start with."""


@dataclass(frozen=True)
class Ceilings:
    """The most any provider may be given, whatever its manifest asks, and what it
    gets when it asks for nothing."""

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
    state_dir: Path = Path("/var/lib/musizerr-gateway")
    # Shared with the application and the workers: the control socket and, under
    # ``providers/<name>/``, the socket of each provider.
    run_dir: Path = Path("/run/musizerr")
    # The host's workspace directory (its ``workspace_dir`` setting): each sandbox
    # sees only ``<workspace_dir>/<provider>``, at ``/workspace``.
    workspace_dir: Path = Path("/downloads/.musizerr-work")
    # The uid/gid the provider sees itself as inside its sandbox (the only one).
    sandbox_uid: int = 10001
    sandbox_gid: int = 10001
    ceilings: Ceilings = Ceilings()
    # The folders of the host a provider may be given a path inside of (``filesystem_from_settings``,
    # see ``filesystem.py``): absolute, normalized, never "/". Empty: no folder is ever granted.
    filesystem_roots: tuple[str, ...] = ()

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


def from_environment(environ: dict[str, str] | None = None) -> GatewayConfig:
    env = os.environ if environ is None else environ
    defaults = GatewayConfig()
    ceilings = Ceilings(
        memory_mb=int(env.get("GATEWAY_MAX_MEMORY_MB", defaults.ceilings.memory_mb)),
        open_files=int(env.get("GATEWAY_MAX_OPEN_FILES", defaults.ceilings.open_files)),
        processes=int(env.get("GATEWAY_MAX_PROCESSES", defaults.ceilings.processes)),
    )
    state_dir = Path(env.get("GATEWAY_STATE_DIR", defaults.state_dir))
    run_dir = Path(env.get("GATEWAY_RUN_DIR", defaults.run_dir))
    return GatewayConfig(
        state_dir=state_dir,
        run_dir=run_dir,
        workspace_dir=Path(env.get("GATEWAY_WORKSPACE_DIR", defaults.workspace_dir)),
        sandbox_uid=int(env.get("GATEWAY_SANDBOX_UID", defaults.sandbox_uid)),
        sandbox_gid=int(env.get("GATEWAY_SANDBOX_GID", defaults.sandbox_gid)),
        ceilings=ceilings,
        filesystem_roots=_roots(env.get("GATEWAY_FILESYSTEM_ROOTS", ""), (state_dir, run_dir)),
    )


def _roots(text: str, own: tuple[Path, ...]) -> tuple[str, ...]:
    """``GATEWAY_FILESYSTEM_ROOTS``: folders separated by ``:``. A root that could expose the gateway
    itself is a configuration error, not something to skip quietly."""
    roots: list[str] = []
    for item in (part.strip() for part in text.split(":")):
        if not item:
            continue
        try:
            root = check_root(item)
        except PathRefused as exc:
            raise ConfigError(f"GATEWAY_FILESYSTEM_ROOTS: {exc}") from exc
        for path in own:
            if inside(root, str(path)) or inside(str(path), root):
                raise ConfigError(f"GATEWAY_FILESYSTEM_ROOTS: '{root}' overlaps the gateway's own folder {path}")
        if root not in roots:
            roots.append(root)
    return tuple(roots)
