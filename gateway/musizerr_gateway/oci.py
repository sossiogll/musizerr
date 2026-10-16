"""
A minimal OCI distribution client: what the gateway needs to pull ONE image by
digest from a public registry, with the standard library only.

- **Bearer tokens**, the flow GHCR and Docker Hub use: a ``401`` with a
  ``WWW-Authenticate: Bearer realm=...`` challenge, a token fetched from the realm, the
  request repeated with it. Anonymous unless the entry brings a **credential** (the
  token of a private source): then the token is asked for with HTTP Basic
  (``username:token``; a bare token is sent with a placeholder username), which is the
  standard Docker token authentication, and a registry that challenges with Basic
  gets it directly. The credential goes to the registry's host and to its token realm
  and nowhere else (a realm on another registrable domain is refused, and so is a
  redirect that leaves the host), only over https, and it is never put in a message.
- **Plain HTTP** only for a registry the desired entry marks as insecure, and then no
  credential.
- **Everything is verified.** The manifest of an entry must hash to the digest the
  entry pins; the manifest chosen from an index, the config and every layer must hash
  to the digest their descriptor names, and have the size it names. A mismatch is an
  :class:`ImageError` and nothing of the image is used.
- Redirects (blobs usually are served by a CDN) are followed by hand, and the
  ``Authorization`` header is dropped when the redirect leaves the registry's host.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import platform as _platform
import re
import socket
import ssl
from dataclasses import dataclass, field
from typing import BinaryIO, Mapping, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from .desired import DesiredProvider

MEDIA_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MEDIA_DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MEDIA_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"

INDEX_TYPES = frozenset({MEDIA_OCI_INDEX, MEDIA_DOCKER_LIST})
MANIFEST_TYPES = frozenset({MEDIA_OCI_MANIFEST, MEDIA_DOCKER_MANIFEST})
GZIP_LAYER_TYPES = frozenset({
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
})
TAR_LAYER_TYPES = frozenset({"application/vnd.oci.image.layer.v1.tar"})
LAYER_TYPES = GZIP_LAYER_TYPES | TAR_LAYER_TYPES
ACCEPT = ", ".join(sorted(INDEX_TYPES | MANIFEST_TYPES))

MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_LAYER_BYTES = 4 * 1024 * 1024 * 1024
MAX_LAYERS = 128
_MAX_REDIRECTS = 5
_PLACEHOLDER_USER = "musizerr"
_CHUNK = 256 * 1024
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ImageError(Exception):
    """The image is not what it was pinned to be, or cannot be used: refused, with a reason."""


class RegistryError(Exception):
    """The registry could not be reached or answered with an error: worth trying again later."""


# ------------------------------------------------------------------
# HTTP
# ------------------------------------------------------------------


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: BinaryIO

    def close(self) -> None:
        close = getattr(self.body, "close", None)
        if close is not None:
            close()


class Http(Protocol):
    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse: ...


class StdlibHttp:
    """One GET at a time over ``http.client``, redirects NOT followed."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise RegistryError(f"'{url}' is not an http(s) URL")
        if parts.scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                parts.hostname, parts.port, timeout=self._timeout, context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=self._timeout)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        try:
            connection.request("GET", target, headers={"User-Agent": "musizerr-gateway", **headers})
            response = connection.getresponse()
        except (OSError, http.client.HTTPException, ssl.SSLError, socket.timeout) as exc:
            connection.close()
            raise RegistryError(f"{parts.hostname}: {exc}") from exc
        return HttpResponse(response.status, {k.lower(): v for k, v in response.getheaders()}, _Closing(response, connection))


class _Closing:
    """A response body that closes its connection with it."""

    def __init__(self, response: http.client.HTTPResponse, connection: http.client.HTTPConnection) -> None:
        self._response = response
        self._connection = connection

    def read(self, size: int = -1) -> bytes:
        try:
            return self._response.read(size)
        except (OSError, http.client.HTTPException, socket.timeout) as exc:
            raise RegistryError(f"the download broke off: {exc}") from exc

    def close(self) -> None:
        self._response.close()
        self._connection.close()


# ------------------------------------------------------------------
# What an image is
# ------------------------------------------------------------------


@dataclass(frozen=True)
class Layer:
    digest: str
    size: int
    media_type: str


@dataclass(frozen=True)
class ImageConfig:
    labels: dict[str, str] = field(default_factory=dict)
    entrypoint: tuple[str, ...] = ()
    cmd: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    diff_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedImage:
    """The image behind a pinned digest: the manifest of the platform that was
    chosen (its digest is the identity of the unpacked root filesystem), its config
    and its layers, base first."""

    manifest_digest: str
    config: ImageConfig
    layers: tuple[Layer, ...]


def host_architecture() -> str:
    machine = _platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


def sha256_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json_object(data: bytes, what: str) -> dict:
    try:
        value = json.loads(data)
    except ValueError as exc:
        raise ImageError(f"{what} is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ImageError(f"{what} is not a JSON object")
    return value


def _descriptor(raw: object, what: str) -> tuple[str, int, str]:
    if not isinstance(raw, dict):
        raise ImageError(f"{what} is not a descriptor")
    digest, size, media = raw.get("digest"), raw.get("size"), raw.get("mediaType")
    if not isinstance(digest, str) or not _DIGEST.match(digest):
        raise ImageError(f"{what} has no sha256 digest")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ImageError(f"{what} has no size")
    return digest, size, media if isinstance(media, str) else ""


def parse_config(data: bytes) -> ImageConfig:
    document = _json_object(data, "the image config")
    section = document.get("config") or {}
    if not isinstance(section, dict):
        raise ImageError("the image config has no 'config' section")
    labels = section.get("Labels") or {}
    if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
        raise ImageError("the image labels are not strings")
    env: dict[str, str] = {}
    for item in section.get("Env") or ():
        if isinstance(item, str) and "=" in item:
            key, _, value = item.partition("=")
            env[key] = value

    def command(name: str) -> tuple[str, ...]:
        value = section.get(name) or []
        if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
            raise ImageError(f"the image '{name}' is not a list of strings")
        return tuple(value)

    diff_ids = tuple((document.get("rootfs") or {}).get("diff_ids") or ())
    if not all(isinstance(d, str) and _DIGEST.match(d) for d in diff_ids):
        raise ImageError("the image config has malformed diff_ids")
    return ImageConfig(labels, command("Entrypoint"), command("Cmd"), env, diff_ids)


# ------------------------------------------------------------------
# The registry
# ------------------------------------------------------------------


def _challenge(header: str) -> tuple[str, dict[str, str]]:
    scheme, _, rest = header.strip().partition(" ")
    return scheme.lower(), dict(re.findall(r'(\w+)="([^"]*)"', rest))


class RegistryClient:
    """The pulls from one repository of one registry."""

    def __init__(
        self, entry: DesiredProvider, http: Http | None = None, architecture: str | None = None, credential: str | None = None,
    ) -> None:
        if credential and entry.insecure:
            raise ImageError("a credential is never sent over plain HTTP")
        self._credential = credential or None
        self._entry = entry
        self._http = http or StdlibHttp()
        self._architecture = architecture or host_architecture()
        self._scheme = "http" if entry.insecure else "https"
        self._base = f"{self._scheme}://{entry.registry}/v2/{entry.repository}"
        self._authorization: str | None = None  # the header the registry's own requests carry

    # ---- transport ------------------------------------------------

    def _open(self, url: str, accept: str | None = None) -> HttpResponse:
        """A 200 response for ``url`` (authentication and redirects handled)."""
        headers = {"Accept": accept} if accept else {}
        registry_host = urlsplit(url).netloc
        authenticated = False
        for _ in range(_MAX_REDIRECTS + 1):
            sent = dict(headers)
            on_registry = urlsplit(url).netloc == registry_host
            if on_registry and self._authorization:
                sent["Authorization"] = self._authorization
            response = self._http.get(url, sent)
            if response.status == 401 and on_registry and not authenticated:
                challenge = response.headers.get("www-authenticate", "")
                response.close()
                self._authenticate(challenge)  # also when a token was held: it may have expired
                authenticated = True
                continue
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise RegistryError("a redirect without a location")
                target = urljoin(url, location)
                if urlsplit(target).scheme != self._scheme:
                    raise ImageError(f"refusing a redirect from {self._scheme} to {urlsplit(target).scheme}")
                url = target
                continue
            if response.status == 200:
                return response
            response.close()
            if response.status in (401, 403):
                what = "the credential" if self._credential else "anonymous access"
                raise ImageError(f"the registry refuses {what} to {self._entry.repository} ({response.status})")
            if response.status == 404:
                raise ImageError(f"not found in the registry: {urlsplit(url).path}")
            raise RegistryError(f"the registry answered {response.status} for {urlsplit(url).path}")
        raise RegistryError("too many redirects")

    def _basic(self) -> str:
        """The HTTP Basic header of the credential: ``user:secret`` as it is, a bare token under a
        placeholder user name (registries that take a token ignore the user name)."""
        assert self._credential is not None
        pair = self._credential if ":" in self._credential else f"{_PLACEHOLDER_USER}:{self._credential}"
        return "Basic " + base64.b64encode(pair.encode()).decode()

    def _authenticate(self, challenge: str) -> None:
        """Answer a ``401``: fetch a bearer token (the credential, if there is one, goes to the
        token service only) or, for a registry that challenges with Basic, send the credential
        with every request to the registry."""
        scheme, params = _challenge(challenge)
        if scheme == "basic":
            if not self._credential:
                raise ImageError("the registry asks for credentials and this source has none")
            self._authorization = self._basic()
            return
        self._authorization = "Bearer " + self._fetch_token(scheme, params)

    def _fetch_token(self, scheme: str, params: dict[str, str]) -> str:
        if scheme != "bearer" or "realm" not in params:
            raise ImageError(f"the registry asks for credentials ({scheme or 'no challenge'}) this client cannot give")
        realm = urlsplit(params["realm"])
        if realm.scheme != self._scheme or not realm.hostname:
            raise ImageError(f"the token realm '{params['realm']}' is not an {self._scheme} URL")
        if not self._entry.insecure and _is_internal_literal(realm.hostname):
            raise ImageError(f"the token realm '{realm.hostname}' is an internal address")
        headers: dict[str, str] = {}
        if self._credential:
            if not _realm_belongs_to(self._entry.registry, realm.hostname):
                raise ImageError(
                    f"the token realm '{realm.hostname}' is not the registry's own: "
                    "the credential is not sent to it"
                )
            headers["Authorization"] = self._basic()
        query = dict(parse_qsl(realm.query))
        if "service" in params:
            query["service"] = params["service"]
        query["scope"] = params.get("scope") or f"repository:{self._entry.repository}:pull"
        url = realm._replace(query=urlencode(query)).geturl()
        response = self._http.get(url, headers)
        try:
            if response.status in (401, 403) and self._credential:
                raise ImageError(f"the token service refused the credential ({response.status})")
            if response.status != 200:
                raise ImageError(f"the token service answered {response.status}")
            document = _json_object(response.body.read(MAX_JSON_BYTES), "the token")
        finally:
            response.close()
        token = document.get("token") or document.get("access_token")
        if not isinstance(token, str) or not token:
            raise ImageError("the token service gave no token")
        return token

    # ---- content --------------------------------------------------

    def _fetch_json_blob(self, url: str, digest: str, what: str, accept: str | None = None) -> bytes:
        response = self._open(url, accept)
        try:
            data = response.body.read(MAX_JSON_BYTES + 1)
        finally:
            response.close()
        if len(data) > MAX_JSON_BYTES:
            raise ImageError(f"{what} is over {MAX_JSON_BYTES} bytes")
        actual = sha256_of(data)
        if actual != digest:
            raise ImageError(f"{what} hashes to {actual}, not to the {digest} it was pinned to")
        return data

    def fetch_manifest(self, digest: str) -> tuple[bytes, str]:
        """The manifest or index with this digest (verified) and its media type."""
        data = self._fetch_json_blob(f"{self._base}/manifests/{digest}", digest, "the manifest", ACCEPT)
        document = _json_object(data, "the manifest")
        media = document.get("mediaType")
        if media is None:  # an OCI manifest may omit it: decide by shape
            media = MEDIA_OCI_INDEX if "manifests" in document else MEDIA_OCI_MANIFEST
        return data, media

    def resolve(self) -> ResolvedImage:
        """The pinned image: its platform manifest, config and layers, every one verified."""
        digest = self._entry.digest
        data, media = self.fetch_manifest(digest)
        if media in INDEX_TYPES:
            digest = self._pick_platform(_json_object(data, "the index"))
            data, media = self.fetch_manifest(digest)
        if media not in MANIFEST_TYPES:
            raise ImageError(f"unsupported manifest type '{media}'")
        manifest = _json_object(data, "the manifest")
        config_digest, _size, _media = _descriptor(manifest.get("config"), "the manifest's config")
        config = parse_config(self._fetch_json_blob(f"{self._base}/blobs/{config_digest}", config_digest, "the config"))
        raw_layers = manifest.get("layers")
        if not isinstance(raw_layers, list) or not raw_layers or len(raw_layers) > MAX_LAYERS:
            raise ImageError("the manifest has no layers, or too many")
        layers = []
        for raw in raw_layers:
            layer_digest, size, layer_media = _descriptor(raw, "a layer")
            if layer_media not in LAYER_TYPES:
                raise ImageError(f"unsupported layer type '{layer_media}'")
            if size > MAX_LAYER_BYTES:
                raise ImageError(f"a layer of {size} bytes is over the limit")
            layers.append(Layer(layer_digest, size, layer_media))
        if len(config.diff_ids) != len(layers):
            raise ImageError("the config and the manifest disagree on the number of layers")
        return ResolvedImage(digest, config, tuple(layers))

    def _pick_platform(self, index: dict) -> str:
        for item in index.get("manifests") or ():
            platform = item.get("platform") if isinstance(item, dict) else None
            if isinstance(platform, dict) and platform.get("os") == "linux" and platform.get("architecture") == self._architecture:
                digest, _size, _media = _descriptor(item, "an index entry")
                return digest
        raise ImageError(f"the image has no linux/{self._architecture} variant")

    def download_blob(self, layer: Layer, out: BinaryIO) -> None:
        """Write the layer to ``out``, verifying its digest and size on the way."""
        response = self._open(f"{self._base}/blobs/{layer.digest}")
        digest = hashlib.sha256()
        size = 0
        try:
            while chunk := response.body.read(_CHUNK):
                size += len(chunk)
                if size > layer.size:
                    raise ImageError(f"layer {layer.digest} is larger than the {layer.size} bytes it declares")
                digest.update(chunk)
                out.write(chunk)
        finally:
            response.close()
        if size != layer.size:
            raise ImageError(f"layer {layer.digest} has {size} bytes, not the {layer.size} it declares")
        actual = "sha256:" + digest.hexdigest()
        if actual != layer.digest:
            raise ImageError(f"layer {layer.digest} hashes to {actual}")


def _is_internal_literal(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not address.is_global


def _realm_belongs_to(registry: str, realm_host: str) -> bool:
    """Whether a token service at ``realm_host`` may receive the credential for ``registry``: the
    same host, a host under it (``auth.ghcr.io``), or a host under the registry's parent domain when
    that is itself a domain with at least two labels (``registry-1.docker.io`` and ``auth.docker.io``; never ``io``)."""
    host = registry.rpartition(":")[0] if registry.count(":") == 1 else registry
    realm_host = realm_host.lower()
    host = host.lower()
    if realm_host == host or realm_host.endswith("." + host):
        return True
    parent = host.partition(".")[2]
    if parent.count(".") < 1:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return realm_host == parent or realm_host.endswith("." + parent)
