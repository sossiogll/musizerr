"""Test doubles for the gateway: an image registry in memory and tar layers built in code."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass, field
from typing import Mapping

from musizeer_gateway.desired import DesiredProvider
from musizeer_gateway.oci import (
    MEDIA_DOCKER_LIST,
    MEDIA_DOCKER_MANIFEST,
    MEDIA_OCI_INDEX,
    MEDIA_OCI_MANIFEST,
    HttpResponse,
)
from musizeer_sdk.interfaces import PROVIDER_API_VERSION
from musizeer_sdk.labels import LABEL_API_VERSION, LABEL_NAME, LABEL_PERMISSIONS, LABEL_ROLES
from musizeer_sdk.permissions import Permissions, canonical

LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
APPROVED = canonical(Permissions(egress=("lrclib.net:443",)))  # what labels_for() declares by default


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Tar:
    """A tar archive under construction (entries in the order they are added)."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        self.tar = tarfile.open(fileobj=self.buffer, mode="w", format=tarfile.PAX_FORMAT)

    def _add(self, name: str, type_: bytes, data: bytes = b"", mode: int = 0o644, linkname: str = "", devmajor: int = 0, devminor: int = 0):
        info = tarfile.TarInfo(name)
        info.type, info.mode, info.linkname, info.mtime = type_, mode, linkname, 1_700_000_000
        info.devmajor, info.devminor = devmajor, devminor
        info.size = len(data) if type_ == tarfile.REGTYPE else 0
        self.tar.addfile(info, io.BytesIO(data) if type_ == tarfile.REGTYPE else None)
        return self

    def file(self, name: str, data: bytes = b"x", mode: int = 0o644):
        return self._add(name, tarfile.REGTYPE, data, mode)

    def dir(self, name: str, mode: int = 0o755):
        return self._add(name, tarfile.DIRTYPE, mode=mode)

    def symlink(self, name: str, target: str):
        return self._add(name, tarfile.SYMTYPE, linkname=target)

    def hardlink(self, name: str, target: str):
        return self._add(name, tarfile.LNKTYPE, linkname=target)

    def device(self, name: str):
        return self._add(name, tarfile.CHRTYPE, devmajor=1, devminor=3)

    def fifo(self, name: str):
        return self._add(name, tarfile.FIFOTYPE)

    def whiteout(self, path: str):
        directory, _, name = path.rpartition("/")
        return self.file(f"{directory + '/' if directory else ''}.wh.{name}", b"")

    def opaque(self, directory: str):
        return self.file(f"{directory}/.wh..wh..opq", b"")

    def raw(self) -> bytes:
        self.tar.close()
        return self.buffer.getvalue()

    def gzipped(self) -> bytes:
        return gzip.compress(self.raw(), mtime=0)


def labels_for(name: str = "demo", roles: str = "lyrics", version: int = PROVIDER_API_VERSION, permissions: str | None = None) -> dict[str, str]:
    return {
        LABEL_NAME: name,
        LABEL_ROLES: roles,
        LABEL_API_VERSION: str(version),
        LABEL_PERMISSIONS: permissions or json.dumps({"egress": ["lrclib.net:443"], "egress_from_settings": [], "filesystem_from_settings": [], "limits": {}}),
    }


@dataclass
class FakeImage:
    layers: list[Tar]
    labels: Mapping[str, str] = field(default_factory=labels_for)
    entrypoint: tuple[str, ...] = ("python", "-m", "musizeer_sdk.runner")
    docker_types: bool = False
    blobs: dict[str, bytes] = field(default_factory=dict)
    manifest: bytes = b""

    def build(self) -> "FakeImage":
        layer_blobs = [layer.gzipped() for layer in self.layers]
        diff_ids = [digest(layer.raw()) for layer in self.layers]
        config = json.dumps({
            "architecture": "amd64", "os": "linux",
            "config": {"Labels": dict(self.labels), "Entrypoint": list(self.entrypoint), "Env": ["PATH=/usr/bin", "SECRET=1"]},
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
        }).encode()
        self.blobs[digest(config)] = config
        for blob in layer_blobs:
            self.blobs[digest(blob)] = blob
        self.manifest = json.dumps({
            "schemaVersion": 2,
            "mediaType": MEDIA_DOCKER_MANIFEST if self.docker_types else MEDIA_OCI_MANIFEST,
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": digest(config), "size": len(config)},
            "layers": [
                {
                    "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip" if self.docker_types else LAYER_GZIP,
                    "digest": digest(blob), "size": len(blob),
                }
                for blob in layer_blobs
            ],
        }).encode()
        return self

    @property
    def manifest_digest(self) -> str:
        return digest(self.manifest)

    @property
    def layer_digests(self) -> list[str]:
        return [d for d in self.blobs if json.loads(self.manifest)["layers"] and d in {l["digest"] for l in json.loads(self.manifest)["layers"]}]


class FakeRegistry:
    """Serves what ``RegistryClient`` asks for from a dict of URL -> response."""

    def __init__(self, registry: str = "registry.test", repository: str = "musizeer/demo", scheme: str = "https") -> None:
        self.base = f"{scheme}://{registry}/v2/{repository}"
        self.registry, self.repository, self.scheme = registry, repository, scheme
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.require_token: str | None = None
        self.token_realm = f"{scheme}://auth.registry.test/token"
        self.redirect_blobs_to: str | None = None
        self.token_calls = 0
        # Private registry: the token service (and, with ``basic_challenge``, the registry itself)
        # wants HTTP Basic with exactly this "user:secret".
        self.require_basic: str | None = None
        self.basic_challenge = False

    def add_image(self, image: FakeImage, as_index_for: str | None = None, extra_index_entries: list | None = None) -> str:
        """Publish ``image``; with ``as_index_for`` an index (that platform + ``extra_index_entries``)
        is the pinned object. Returns the digest the entry should pin."""
        self.routes[f"{self.base}/manifests/{image.manifest_digest}"] = (200, {}, image.manifest)
        for blob_digest, blob in image.blobs.items():
            self.routes[f"{self.base}/blobs/{blob_digest}"] = (200, {}, blob)
        if as_index_for is None:
            return image.manifest_digest
        index = json.dumps({
            "schemaVersion": 2, "mediaType": MEDIA_OCI_INDEX,
            "manifests": [
                *(extra_index_entries or []),
                {"mediaType": MEDIA_OCI_MANIFEST, "digest": image.manifest_digest, "size": len(image.manifest),
                 "platform": {"os": "linux", "architecture": as_index_for}},
            ],
        }).encode()
        self.routes[f"{self.base}/manifests/{digest(index)}"] = (200, {}, index)
        return digest(index)

    def entry(self, digest_: str, name: str = "demo", **kwargs) -> DesiredProvider:
        kwargs.setdefault("insecure", self.scheme == "http")
        kwargs.setdefault("roles", ("lyrics",))
        kwargs.setdefault("permissions", APPROVED)
        return DesiredProvider(name, self.registry, self.repository, digest_, **kwargs)

    def _basic_header(self) -> str:
        import base64

        return "Basic " + base64.b64encode((self.require_basic or "").encode()).decode()

    # ---- Http ------------------------------------------------------

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        self.requests.append((url, dict(headers)))
        if url.startswith(self.token_realm):
            self.token_calls += 1
            if self.require_basic and headers.get("Authorization") != self._basic_header():
                return _response(401, {}, b"denied")
            return _response(200, {}, json.dumps({"token": "T0KEN"}).encode())
        if self.basic_challenge and url.startswith(self.base) and headers.get("Authorization") != self._basic_header():
            return _response(401, {"www-authenticate": 'Basic realm="registry"'}, b"")
        if (self.require_token or self.require_basic) and url.startswith(self.base) and not self.basic_challenge:
            if headers.get("Authorization") != "Bearer " + (self.require_token or "T0KEN"):
                challenge = f'Bearer realm="{self.token_realm}",service="registry.test",scope="repository:{self.repository}:pull"'
                return _response(401, {"www-authenticate": challenge}, b"")
        if self.redirect_blobs_to and url.startswith(f"{self.base}/blobs/"):
            return _response(307, {"location": self.redirect_blobs_to + url.rsplit("/", 1)[1]}, b"")
        if url.startswith(self.redirect_blobs_to or "\0"):
            blob = self.routes.get(f"{self.base}/blobs/{url.rsplit('/', 1)[1]}")
            if blob:
                return _response(*blob)
        route = self.routes.get(url)
        if route is None:
            return _response(404, {}, b"")
        return _response(*route)


def _response(status: int, headers: dict[str, str], body: bytes) -> HttpResponse:
    return HttpResponse(status, {k.lower(): v for k, v in headers.items()}, io.BytesIO(body))
