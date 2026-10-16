"""
From a desired entry to a root filesystem ready to run: resolve the pinned
digest, check that the image describes itself as the entry says, fetch and unpack
the layers (the ones already cached are not fetched), compose the root filesystem.

An image is refused, with a reason, when its labels (``org.musizerr.provider.*``,
read from its config BEFORE any layer is downloaded) disagree with the entry: another
name, other roles, other permissions than the ones the admin approved, a provider API version that is not this gateway's SDK's, or labels
that do not describe a provider at all.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.labels import DeclaredManifest, LabelError, declared_from_labels
from musizerr_sdk.permissions import Permissions, canonical

from .config import GatewayConfig
from .desired import DesiredProvider
from .layers import LayerStore, compose_rootfs
from .oci import Http, ImageConfig, ImageError, RegistryClient

logger = logging.getLogger(__name__)

# The directories the sandbox mounts over: the root filesystem must hold them.
MOUNTPOINTS = ("proc", "dev", "tmp", "workspace", "run/provider", "run/egress")


@dataclass(frozen=True)
class PreparedImage:
    entry: DesiredProvider
    rootfs: Path
    config: ImageConfig
    declared: DeclaredManifest

    @property
    def command(self) -> list[str]:
        return [*self.config.entrypoint, *self.config.cmd]


def verify_labels(entry: DesiredProvider, config: ImageConfig) -> DeclaredManifest:
    """What the image declares, if it is what ``entry`` says; else :class:`ImageError`."""
    try:
        declared = declared_from_labels(config.labels)
    except LabelError as exc:
        raise ImageError(str(exc)) from exc
    if declared.name != entry.name:
        raise ImageError(f"the image declares the provider '{declared.name}', the approved entry says '{entry.name}'")
    if declared.provider_api_version != PROVIDER_API_VERSION:
        raise ImageError(
            f"the image implements provider API version {declared.provider_api_version}, "
            f"this gateway runs version {PROVIDER_API_VERSION}"
        )
    if set(entry.roles) != set(declared.roles):
        raise ImageError(f"the image declares the roles {sorted(declared.roles)}, the approved entry says {sorted(entry.roles)}")
    if canonical(declared.permissions) != entry.permissions:
        raise ImageError(
            "the image declares permissions that are not the ones approved for it "
            "(an image cannot gain a permission nobody approved): " + _permissions_difference(entry.permissions, declared.permissions)
        )
    if not (config.entrypoint or config.cmd):
        raise ImageError("the image has no entry point")
    return declared


class ImageStore:
    def __init__(self, config: GatewayConfig, http: Http | None = None, architecture: str | None = None) -> None:
        self._config = config
        self._http = http
        self._architecture = architecture
        self.layers = LayerStore(config.layers_dir)

    def _image_dir(self, entry: DesiredProvider) -> Path:
        return self._config.images_dir / entry.digest.replace(":", "-")

    def prepare(self, entry: DesiredProvider, credential: str | None = None) -> PreparedImage:
        """The unpacked image pinned by ``entry`` (``credential``, if any, is used to pull it and
        goes no further). Raises :class:`ImageError` (it is not
        what it was pinned to: do not retry) or :class:`RegistryError` (try again later)."""
        directory = self._image_dir(entry)
        cached = directory / "image.json"
        if cached.is_file():
            config = _load_config(cached)
            return PreparedImage(entry, directory / "rootfs", config, verify_labels(entry, config))

        client = RegistryClient(entry, self._http, self._architecture, credential)
        image = client.resolve()
        declared = verify_labels(entry, image.config)
        logger.info("%s: %s, %d layers", entry.name, entry.reference, len(image.layers))
        layer_dirs = [
            self.layers.ensure(layer, diff_id, client.download_blob)
            for layer, diff_id in zip(image.layers, image.config.diff_ids)
        ]
        self._config.images_dir.mkdir(parents=True, exist_ok=True)
        staging = self._config.images_dir / f".compose-{uuid.uuid4().hex}"
        try:
            staging.mkdir()
            compose_rootfs(layer_dirs, staging / "rootfs", MOUNTPOINTS)
            _store_config(staging / "image.json", image.config, image.manifest_digest)
            shutil.rmtree(directory, ignore_errors=True)
            os.rename(staging, directory)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return PreparedImage(entry, directory / "rootfs", image.config, declared)


def _store_config(path: Path, config: ImageConfig, manifest_digest: str) -> None:
    path.write_text(json.dumps({
        "manifest_digest": manifest_digest,
        "labels": config.labels,
        "entrypoint": list(config.entrypoint),
        "cmd": list(config.cmd),
        "env": config.env,
    }), encoding="utf-8")


def _load_config(path: Path) -> ImageConfig:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ImageConfig(
            labels=dict(data["labels"]), entrypoint=tuple(data["entrypoint"]), cmd=tuple(data["cmd"]), env=dict(data["env"]),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ImageError(f"the unpacked image is damaged ({exc}); remove {path.parent} to fetch it again") from exc


def _permissions_difference(approved: Permissions, declared: Permissions) -> str:
    """Which parts of the declaration are not the approved ones (names only: the values are in the image)."""
    declared = canonical(declared)
    parts = [
        name for name in ("egress", "egress_from_settings", "filesystem_from_settings", "tcp_from_settings", "limits")
        if getattr(approved, name) != getattr(declared, name)
    ]
    return ", ".join(parts)
