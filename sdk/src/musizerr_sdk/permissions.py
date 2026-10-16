"""
What a provider asks the sandbox to grant it: the ONLY resources it will have.

A provider declares them as data (``BaseProvider.permissions``); they are part
of its manifest and of its image's labels, so the admin sees them before
installing and the gateway enforces them:

- ``egress``: fixed destinations, ``"host:port"`` (``"lrclib.net:443"``). The host may be a WILDCARD,
  ``"*.example.com:443"``: any subdomain of ``example.com`` on that port, for a service that lives on
  a CDN. It never means ``example.com`` itself (list that on its own if it is needed), at least two
  labels must follow the ``*.`` (``*.com`` is refused) and it is not an IP address;
- ``egress_from_settings``: keys of the provider's settings whose value is a
  destination the USER configures (a Navidrome or Lidarr URL, a host): the
  destination is allowed while the setting holds it, private and Tailscale
  addresses included, because the user typed them;
- ``filesystem_from_settings``: keys of settings whose value is a host path the
  provider may use, read-only or read-write;
- ``tcp_from_settings``: keys of settings whose value names a HOST the provider may open raw TCP
  connections to on ANY port (or a declared range): a protocol that is not HTTP, such as FTP, whose data
  connections are on ports the server chooses. The host is the user's (private addresses included);
  the ports are the declaration's, not the setting's;
- ``limits``: the resources of its process, within the ceilings the gateway
  sets.

A provider that declares nothing (the default) may reach no network, no path
and gets the gateway's default limits.
"""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal
from urllib.parse import urlsplit

FilesystemMode = Literal["ro", "rw"]

# The port a URL scheme implies when it names none.
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21, "ftps": 990, "sftp": 22, "ssh": 22}

# A value that names a host with no scheme and no port (``music.lan``): the web ports.
BARE_HOST_PORTS = (80, 443)

_HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)*$")
_SETTING_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_PORTS = re.compile(r"^([0-9]{1,5})(?:-([0-9]{1,5}))?$")
WILDCARD = "*."


class PermissionsError(ValueError):
    """A permission declaration that makes no sense."""


@dataclass(frozen=True)
class FilesystemAccess:
    setting: str
    mode: FilesystemMode = "ro"


@dataclass(frozen=True)
class TcpAccess:
    """Raw TCP to the host(s) the setting ``setting`` names, on ``ports``: ``"any"`` (1-65535), one port
    (``"2121"``) or a range (``"30000-30100"``). Written as :func:`normalize_ports` writes it."""

    setting: str
    ports: str = "any"


@dataclass(frozen=True)
class ResourceLimits:
    """Limits of the provider's process; ``None`` is the gateway's default. The
    gateway caps each at its own ceiling, whatever the provider asks."""

    memory_mb: int | None = None
    open_files: int | None = None
    processes: int | None = None


@dataclass(frozen=True)
class Permissions:
    egress: tuple[str, ...] = ()
    egress_from_settings: tuple[str, ...] = ()
    filesystem_from_settings: tuple[FilesystemAccess, ...] = ()
    tcp_from_settings: tuple[TcpAccess, ...] = ()
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    @property
    def declares_nothing(self) -> bool:
        return self == Permissions()


NO_PERMISSIONS = Permissions()


# ------------------------------------------------------------------
# Hosts and ports
# ------------------------------------------------------------------


def normalize_host(host: str) -> str:
    """The lowercase host without brackets or a trailing dot; raises
    :class:`PermissionsError` for what is neither a DNS name nor an IP."""
    host = host.strip().lower().strip("[]").rstrip(".")
    if not host:
        raise PermissionsError("empty host")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not _HOSTNAME.match(host):
        raise PermissionsError(f"'{host}' is not a host name")
    return host


def _split_target(entry: object) -> tuple[str, int]:
    if not isinstance(entry, str):
        raise PermissionsError(f"{entry!r} is not a 'host:port' string")
    host, separator, port = entry.strip().rpartition(":")
    if not separator or (":" in host and not host.endswith("]")):
        raise PermissionsError(f"'{entry}' is not 'host:port'")
    if not (port.isascii() and port.isdigit()) or not 0 < int(port) < 65536:
        raise PermissionsError(f"'{entry}' has no valid port")
    return host, int(port)


def parse_target(entry: str) -> tuple[str, int]:
    """``"host:port"`` (``"[::1]:8080"`` for IPv6) as ``(host, port)``. The host is a name or an
    address: a wildcard is not a destination (see :func:`parse_egress`)."""
    host, port = _split_target(entry)
    return normalize_host(host), port


def is_wildcard(host: str) -> bool:
    return host.startswith(WILDCARD)


def parse_egress(entry: str) -> tuple[str, int]:
    """A fixed destination of a manifest (``egress``): ``parse_target`` or, for a wildcard,
    ``("*.example.com", 443)`` = any subdomain of ``example.com`` (not ``example.com`` itself). At least
    two labels must follow the ``*.`` (``*.com`` would be every site of a TLD), the rest is a plain
    DNS name (no further ``*``, not an IP address). Whether ``example.com`` is itself a public suffix
    (``*.co.uk``) cannot be told without a list of them: the admin sees the pattern in words."""
    host, port = _split_target(entry)
    if not host.strip().lower().startswith(WILDCARD):
        return normalize_host(host), port
    suffix = normalize_host(host.strip().lower()[len(WILDCARD):])
    try:
        ipaddress.ip_address(suffix)
    except ValueError:
        pass
    else:
        raise PermissionsError(f"'{entry}': a wildcard cannot be an address")
    if suffix.count(".") < 1:
        raise PermissionsError(f"'{entry}': a wildcard needs at least two labels after '*.' (not a whole top-level domain)")
    return WILDCARD + suffix, port


def wildcard_matches(pattern: str, host: str) -> bool:
    """``host`` (normalized) is a SUBDOMAIN of what the wildcard ``pattern`` (``*.example.com``) names."""
    suffix = pattern[len(WILDCARD) - 1:]  # ".example.com"
    return len(host) > len(suffix) and host.endswith(suffix)


def parse_ports(text: object) -> tuple[int, int]:
    """``"any"``, ``"2121"`` or ``"30000-30100"`` as ``(first, last)``."""
    if not isinstance(text, str):
        raise PermissionsError(f"{text!r} is not a port range")
    if text.strip().lower() == "any":
        return 1, 65535
    found = _PORTS.match(text.strip())
    if not found:
        raise PermissionsError(f"'{text}' is not 'any', a port or a range of ports ('30000-30100')")
    first = int(found.group(1))
    last = int(found.group(2) or first)
    if not 1 <= first <= last <= 65535:
        raise PermissionsError(f"'{text}' is not a valid range of ports")
    return first, last


def normalize_ports(text: object) -> str:
    first, last = parse_ports(text)
    if (first, last) == (1, 65535):
        return "any"
    return str(first) if first == last else f"{first}-{last}"


def format_target(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def targets_from_value(value: Any) -> tuple[tuple[str, int], ...]:
    """The destinations a setting's VALUE names: a URL (its port, or its scheme's
    default), ``host:port``, a bare host (the web ports), or a list of those.
    What cannot be read as a destination names none: it grants nothing."""
    if isinstance(value, (list, tuple)):
        found: list[tuple[str, int]] = []
        for item in value:
            found.extend(targets_from_value(item))
        return tuple(dict.fromkeys(found))
    if not isinstance(value, str) or not value.strip():
        return ()
    text = value.strip()
    try:
        if "://" in text:
            parts = urlsplit(text)
            if not parts.hostname:
                return ()
            port = parts.port or DEFAULT_PORTS.get(parts.scheme.lower())
            return ((normalize_host(parts.hostname), port),) if port else ()
        host, _, port = text.partition("/")[0].rpartition(":")
        if host and port.isdigit() and (":" not in host or host.endswith("]")):
            return ((normalize_host(host), int(port)),) if 0 < int(port) < 65536 else ()
        bare = text.partition("/")[0]
        host = normalize_host(bare)
    except (PermissionsError, ValueError):
        return ()
    return tuple((host, port) for port in BARE_HOST_PORTS)


def settings_targets(permissions: Permissions, settings: dict[str, Any]) -> tuple[str, ...]:
    """The ``host:port`` destinations ``permissions.egress_from_settings`` name in
    the CURRENT ``settings``."""
    found: list[str] = []
    for key in permissions.egress_from_settings:
        found.extend(format_target(host, port) for host, port in targets_from_value(settings.get(key)))
    return tuple(dict.fromkeys(found))


def tcp_hosts(permissions: Permissions, settings: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """For each setting ``permissions.tcp_from_settings`` names, the HOSTS its current value names (the
    port of a URL or ``host:port`` is not kept: the ports are the declaration's). A setting that names
    no host is left out."""
    found: dict[str, tuple[str, ...]] = {}
    for access in permissions.tcp_from_settings:
        hosts = tuple(dict.fromkeys(host for host, _ in targets_from_value(settings.get(access.setting))))
        if hosts:
            found[access.setting] = hosts
    return found


def path_values(permissions: Permissions, settings: dict[str, Any]) -> dict[str, str]:
    """For each setting ``permissions.filesystem_from_settings`` names, its current value when it is
    text (what the gateway then judges); a setting that holds no text is left out."""
    found: dict[str, str] = {}
    for access in permissions.filesystem_from_settings:
        value = settings.get(access.setting)
        if isinstance(value, str) and value:
            found[access.setting] = value
    return found


# ------------------------------------------------------------------
# Validation and the JSON form (image labels)
# ------------------------------------------------------------------


def validate(permissions: Permissions) -> Permissions:
    """``permissions`` if it is well formed; else :class:`PermissionsError`."""
    for entry in permissions.egress:
        parse_egress(entry)
    for key in (
        *permissions.egress_from_settings, *(a.setting for a in permissions.filesystem_from_settings),
        *(a.setting for a in permissions.tcp_from_settings),
    ):
        if not isinstance(key, str) or not _SETTING_KEY.match(key):
            raise PermissionsError(f"'{key}' is not a setting key")
    for access in permissions.filesystem_from_settings:
        if access.mode not in ("ro", "rw"):
            raise PermissionsError(f"filesystem mode '{access.mode}' is neither 'ro' nor 'rw'")
    for tcp in permissions.tcp_from_settings:
        parse_ports(tcp.ports)
    for name in ("memory_mb", "open_files", "processes"):
        value = getattr(permissions.limits, name)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
            raise PermissionsError(f"limit '{name}' must be a positive integer")
    return permissions


def canonical(permissions: Permissions) -> Permissions:
    """``permissions`` with every list sorted and de-duplicated: two declarations that grant the
    same things are equal once canonical, whatever order they were written in."""
    return Permissions(
        egress=tuple(sorted(set(permissions.egress))),
        egress_from_settings=tuple(sorted(set(permissions.egress_from_settings))),
        filesystem_from_settings=tuple(sorted(set(permissions.filesystem_from_settings), key=lambda a: (a.setting, a.mode))),
        tcp_from_settings=tuple(
            sorted({TcpAccess(a.setting, normalize_ports(a.ports)) for a in permissions.tcp_from_settings}, key=lambda a: (a.setting, a.ports)),
        ),
        limits=permissions.limits,
    )


def to_json(permissions: Permissions) -> str:
    """The compact, sorted JSON of ``permissions`` (what an image label holds)."""
    data = {
        "egress": list(permissions.egress),
        "egress_from_settings": list(permissions.egress_from_settings),
        "filesystem_from_settings": [
            {"setting": access.setting, "mode": access.mode} for access in permissions.filesystem_from_settings
        ],
        "tcp_from_settings": [{"setting": access.setting, "ports": access.ports} for access in permissions.tcp_from_settings],
        "limits": {name: getattr(permissions.limits, name) for name in ("memory_mb", "open_files", "processes")},
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def from_json(text: str) -> Permissions:
    """The inverse of :func:`to_json`, validated; :class:`PermissionsError` for
    anything else."""
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise TypeError("not an object")
        limits = data.get("limits") or {}
        permissions = Permissions(
            egress=_strings(data.get("egress")),
            egress_from_settings=_strings(data.get("egress_from_settings")),
            filesystem_from_settings=tuple(
                FilesystemAccess(setting=item["setting"], mode=item.get("mode", "ro"))
                for item in data.get("filesystem_from_settings") or ()
            ),
            tcp_from_settings=tuple(
                TcpAccess(setting=item["setting"], ports=item.get("ports", "any")) for item in data.get("tcp_from_settings") or ()
            ),
            limits=ResourceLimits(
                memory_mb=limits.get("memory_mb"), open_files=limits.get("open_files"), processes=limits.get("processes"),
            ),
        )
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PermissionsError(f"unreadable permissions: {exc}") from exc
    return validate(permissions)


def _strings(value: Iterable[Any] | None) -> tuple[str, ...]:
    items = tuple(value or ())
    if not all(isinstance(item, str) for item in items):
        raise TypeError("a list of strings is expected")
    return items
