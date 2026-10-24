"""Permissions: what a provider asks the sandbox for, and how an image says it."""
from __future__ import annotations

import io
import json
import os
import socket
import threading
from contextlib import redirect_stdout

import pytest

from musizeer_sdk import runner
from musizeer_sdk.context import StaticProviderContext
from musizeer_sdk.interfaces import PROVIDER_API_VERSION, LyricsProvider
from musizeer_sdk.labels import (
    LABEL_API_VERSION,
    LABEL_LABEL,
    LABEL_NAME,
    LABEL_PERMISSIONS,
    LABEL_ROLES,
    LabelError,
    declared_from_labels,
    labels_for,
)
from musizeer_sdk.manifest import ManifestError, build
from musizeer_sdk.permissions import (
    NO_PERMISSIONS,
    canonical,
    FilesystemAccess,
    Permissions,
    PermissionsError,
    ResourceLimits,
    TcpAccess,
    format_target,
    from_json,
    is_wildcard,
    normalize_ports,
    parse_egress,
    parse_ports,
    parse_target,
    path_values,
    settings_targets,
    targets_from_value,
    tcp_hosts,
    to_json,
    validate,
    wildcard_matches,
)
from sample_providers import SampleLyrics


class Declaring(SampleLyrics):
    name = "declaring"
    provider_api_version = PROVIDER_API_VERSION
    permissions = Permissions(
        egress=("api.example.org:443",),
        egress_from_settings=("url",),
        filesystem_from_settings=(FilesystemAccess("root_dir", "rw"),),
        tcp_from_settings=(TcpAccess("host", "any"),),
        limits=ResourceLimits(memory_mb=256),
    )


# ------------------------------------------------------------------
# Destinations
# ------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [
    ("https://music.example.org", (("music.example.org", 443),)),
    ("http://music.example.org", (("music.example.org", 80),)),
    ("http://192.168.1.5:4533/rest", (("192.168.1.5", 4533),)),
    ("https://Music.Example.ORG:8443/", (("music.example.org", 8443),)),
    ("ftp://files.lan", (("files.lan", 21),)),
    ("music.lan:4533", (("music.lan", 4533),)),
    ("music.lan", (("music.lan", 80), ("music.lan", 443))),
    ("100.64.0.7", (("100.64.0.7", 80), ("100.64.0.7", 443))),
    ("[fd7a:115c::1]:4533", (("fd7a:115c::1", 4533),)),
    ("http://[::1]:9000", (("::1", 9000),)),
    (["http://a.lan:1", "b.lan:2", "http://a.lan:1"], (("a.lan", 1), ("b.lan", 2))),
])
def test_a_setting_value_names_the_destinations_it_means(value, expected):
    assert targets_from_value(value) == expected


@pytest.mark.parametrize("value", [None, "", "  ", 12, "http://", "not a host", "host:99999", "-bad-.lan", "x" * 300, {"a": 1}])
def test_a_value_that_names_no_destination_grants_nothing(value):
    assert targets_from_value(value) == ()


def test_the_destinations_of_the_settings_a_provider_names_follow_their_current_values():
    permissions = Permissions(egress_from_settings=("url", "host", "other"))

    assert settings_targets(permissions, {"url": "https://a.example", "host": "b.lan:21", "ignored": "c.example"}) == (
        "a.example:443", "b.lan:21",
    )
    assert settings_targets(permissions, {}) == ()
    assert settings_targets(NO_PERMISSIONS, {"url": "https://a.example"}) == ()


def test_a_target_is_host_and_port():
    assert parse_target("Lrclib.NET:443") == ("lrclib.net", 443)
    assert parse_target("[::1]:80") == ("::1", 80)
    assert format_target("::1", 80) == "[::1]:80"
    assert format_target("lrclib.net", 443) == "lrclib.net:443"
    for bad in ("lrclib.net", "lrclib.net:0", "lrclib.net:70000", ":443", "a b:443", "a:b:443", 7):
        with pytest.raises(PermissionsError):
            parse_target(bad)


# ------------------------------------------------------------------
# Declaration
# ------------------------------------------------------------------


def test_a_provider_declares_nothing_by_default():
    assert SampleLyrics.permissions is NO_PERMISSIONS
    assert build(SampleLyrics(StaticProviderContext())).permissions.declares_nothing


def test_the_manifest_carries_the_permissions_the_provider_declares():
    assert build(Declaring(StaticProviderContext())).permissions == Declaring.permissions


def test_a_malformed_declaration_is_refused():
    for bad in (
        Permissions(egress=("no-port",)),
        Permissions(egress_from_settings=("bad key",)),
        Permissions(filesystem_from_settings=(FilesystemAccess("root", "wx"),)),  # type: ignore[arg-type]
        Permissions(limits=ResourceLimits(memory_mb=0)),
        Permissions(limits=ResourceLimits(open_files=True)),  # type: ignore[arg-type]
    ):
        with pytest.raises(PermissionsError):
            validate(bad)

    class Broken(Declaring):
        permissions = Permissions(egress=("no-port",))

    with pytest.raises(ValueError):
        build(Broken(StaticProviderContext()))


def test_permissions_survive_their_json_form():
    assert from_json(to_json(Declaring.permissions)) == Declaring.permissions
    assert from_json(to_json(NO_PERMISSIONS)) == NO_PERMISSIONS
    assert to_json(Declaring.permissions) == to_json(from_json(to_json(Declaring.permissions)))


@pytest.mark.parametrize("text", ["", "[]", "{", '{"egress": [1]}', '{"egress": ["a:1"], "limits": {"memory_mb": -1}}',
                                  '{"filesystem_from_settings": [{"mode": "ro"}]}', '{"egress": "ab"}'])
def test_unreadable_permissions_are_refused(text):
    with pytest.raises(PermissionsError):
        from_json(text)


# ------------------------------------------------------------------
# Image labels
# ------------------------------------------------------------------


def test_an_image_says_what_its_provider_declares():
    manifest = build(Declaring(StaticProviderContext()))

    declared = declared_from_labels(labels_for(manifest))

    assert (declared.name, declared.roles, declared.provider_api_version) == ("declaring", ("lyrics",), PROVIDER_API_VERSION)
    assert declared.permissions == Declaring.permissions
    assert labels_for(manifest)[LABEL_LABEL] == manifest.label, "the name to show travels with the image"


def test_a_provider_that_does_not_declare_its_contract_version_has_no_labels():
    with pytest.raises(LabelError, match="provider_api_version"):
        labels_for(build(SampleLyrics(StaticProviderContext())))


@pytest.mark.parametrize("change, message", [
    ({LABEL_NAME: None}, "lacks the labels"),
    ({LABEL_ROLES: "lyrics,teleport"}, "roles"),
    ({LABEL_ROLES: ""}, "roles"),
    ({LABEL_API_VERSION: "five"}, "API version"),
    ({LABEL_PERMISSIONS: "{"}, "unreadable"),
])
def test_labels_that_do_not_describe_a_provider_are_refused(change, message):
    labels = labels_for(build(Declaring(StaticProviderContext())))
    for key, value in change.items():
        if value is None:
            del labels[key]
        else:
            labels[key] = value

    with pytest.raises(LabelError, match=message):
        declared_from_labels(labels)
    with pytest.raises(LabelError):
        declared_from_labels(None)


# ------------------------------------------------------------------
# The runner's command line and the way out of a sandbox
# ------------------------------------------------------------------


def test_the_command_line_prints_the_labels_of_a_provider():
    out = io.StringIO()
    with redirect_stdout(out):
        assert runner.main(["--provider", f"{__name__}:Declaring", "--print-labels"]) == 0

    printed = dict(line.split("=", 1) for line in out.getvalue().splitlines())
    assert printed == labels_for(build(Declaring(StaticProviderContext())))


def test_a_provider_spec_is_module_and_class():
    assert runner.load_provider_class(f"{__name__}:Declaring") is Declaring
    for bad in ("nothing", ":X", "x:"):
        with pytest.raises(SystemExit):
            runner.load_provider_class(bad)
    assert issubclass(Declaring, LyricsProvider)


def test_the_egress_relay_forwards_the_loopback_to_the_proxy_socket(tmp_path):
    path = str(tmp_path / "egress.sock")
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    upstream.bind(path)
    upstream.listen(1)

    def echo():
        connection, _ = upstream.accept()
        with connection:
            while data := connection.recv(100):
                connection.sendall(data.upper())

    threading.Thread(target=echo, daemon=True).start()
    relay = runner.EgressRelay(path)
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    try:
        host, port = relay.server_address
        assert host == "127.0.0.1"
        with socket.create_connection((host, port), timeout=5) as client:
            client.sendall(b"hello")
            assert client.recv(100) == b"HELLO"
    finally:
        relay.shutdown()
        relay.server_close()
        upstream.close()


def test_the_provider_is_pointed_at_the_relay_and_nothing_is_exempt(tmp_path):
    relay = runner.EgressRelay(str(tmp_path / "nowhere.sock"))
    environ = {"NO_PROXY": "internal"}
    try:
        runner.route_through(relay, environ)
    finally:
        relay.server_close()

    for name in runner.PROXY_VARIABLES:
        assert environ[name] == relay.url
    assert environ["NO_PROXY"] == environ["no_proxy"] == ""
    assert os.environ.get("HTTPS_PROXY") != relay.url, "the process environment was not touched"


def test_canonical_permissions_do_not_depend_on_the_order_or_repeats():
    one = Permissions(
        egress=("b.example:443", "a.example:443", "a.example:443"),
        egress_from_settings=("url", "host"),
        filesystem_from_settings=(FilesystemAccess("root", "rw"), FilesystemAccess("cache", "ro")),
        limits=ResourceLimits(memory_mb=64),
    )
    other = Permissions(
        egress=("a.example:443", "b.example:443"),
        egress_from_settings=("host", "url"),
        filesystem_from_settings=(FilesystemAccess("cache", "ro"), FilesystemAccess("root", "rw")),
        limits=ResourceLimits(memory_mb=64),
    )

    assert one != other and canonical(one) == canonical(other)
    assert canonical(canonical(one)) == canonical(one)
    assert canonical(Permissions(egress=("a.example:443",))) != canonical(Permissions(egress=("b.example:443",)))


# ------------------------------------------------------------------
# Wildcard egress: any subdomain on a port
# ------------------------------------------------------------------


@pytest.mark.parametrize("entry, expected", [
    ("*.cdn.example.com:443", ("*.cdn.example.com", 443)),
    ("*.Example.COM:8443", ("*.example.com", 8443)),
    ("*.example.com.:443", ("*.example.com", 443)),
    ("lrclib.net:443", ("lrclib.net", 443)),
    ("1.2.3.4:80", ("1.2.3.4", 80)),
])
def test_an_egress_entry_may_be_a_wildcard_of_subdomains(entry, expected):
    assert parse_egress(entry) == expected
    validate(Permissions(egress=(entry,)))


@pytest.mark.parametrize("entry", [
    "*:443", "*.com:443", "*.:443", "*:*", "*.example.com", "*.example.com:0", "*.example.com:99999",
    "a.*.example.com:443", "*.*.example.com:443", "**.example.com:443", "*example.com:443", "*.exa*mple.com:443",
    "*.1.2.3.4:443", "*.[::1]:443", "* .example.com:443", "*..example.com:443",
])
def test_a_wildcard_that_is_too_wide_or_malformed_is_refused(entry):
    with pytest.raises(PermissionsError):
        parse_egress(entry)
    with pytest.raises(PermissionsError):
        validate(Permissions(egress=(entry,)))
    with pytest.raises(PermissionsError):
        from_json(json.dumps({"egress": [entry]}))


def test_a_target_is_never_a_wildcard():
    with pytest.raises(PermissionsError):
        parse_target("*.example.com:443")


def test_a_wildcard_matches_subdomains_only():
    assert wildcard_matches("*.example.com", "a.example.com") and wildcard_matches("*.example.com", "a.b.example.com")
    assert not wildcard_matches("*.example.com", "example.com")
    assert not wildcard_matches("*.example.com", "badexample.com") and not wildcard_matches("*.example.com", "example.com.evil.org")
    assert not wildcard_matches("*.example.com", "1.2.3.4") and not wildcard_matches("*.example.com", ".example.com")
    assert is_wildcard("*.example.com") and not is_wildcard("example.com")


@pytest.mark.parametrize("value", ["https://*.example.com", "*.example.com:443", "*.example.com", "https://*.example.com:8443/x", ["*.example.com"]])
def test_a_setting_can_never_name_a_wildcard(value):
    assert targets_from_value(value) == ()
    assert settings_targets(Permissions(egress_from_settings=("url",)), {"url": value}) == ()
    assert tcp_hosts(Permissions(tcp_from_settings=(TcpAccess("url"),)), {"url": value}) == {}


def test_wildcards_survive_labels_and_json():
    permissions = Permissions(egress=("*.cdn.example.com:443", "example.com:443"))

    assert from_json(to_json(permissions)) == permissions
    assert declared_from_labels(labels_for(build(_declaring(permissions)))).permissions == permissions


# ------------------------------------------------------------------
# Raw TCP to the host a setting names
# ------------------------------------------------------------------


@pytest.mark.parametrize("text, expected, normalized", [
    ("any", (1, 65535), "any"), ("ANY", (1, 65535), "any"), ("1-65535", (1, 65535), "any"),
    ("2121", (2121, 2121), "2121"), ("2121-2121", (2121, 2121), "2121"), ("30000-30100", (30000, 30100), "30000-30100"),
    (" 21 ", (21, 21), "21"),
])
def test_ports_are_any_one_port_or_a_range(text, expected, normalized):
    assert parse_ports(text) == expected and normalize_ports(text) == normalized


@pytest.mark.parametrize("text", ["", "0", "65536", "30100-30000", "0-10", "1-65536", "a", "21,22", "21-", "-21", "any-21", 21, None, "١٢"])
def test_ports_that_make_no_sense_are_refused(text):
    with pytest.raises(PermissionsError):
        parse_ports(text)


def test_the_tcp_permission_is_validated_canonical_and_survives_json():
    permissions = Permissions(tcp_from_settings=(TcpAccess("host", "any"), TcpAccess("alt", "1-65535"), TcpAccess("data", "30000-30100")))

    assert validate(permissions) is permissions
    assert canonical(permissions).tcp_from_settings == (TcpAccess("alt", "any"), TcpAccess("data", "30000-30100"), TcpAccess("host", "any"))
    assert from_json(to_json(permissions)) == permissions
    assert json.loads(to_json(permissions))["tcp_from_settings"][0] == {"ports": "any", "setting": "host"}
    assert not permissions.declares_nothing
    for bad in (Permissions(tcp_from_settings=(TcpAccess("bad key"),)), Permissions(tcp_from_settings=(TcpAccess("host", "0"),))):
        with pytest.raises(PermissionsError):
            validate(bad)


def test_an_older_declaration_without_tcp_still_reads():
    old = '{"egress":["a.example:443"],"egress_from_settings":[],"filesystem_from_settings":[],"limits":{}}'

    assert from_json(old) == Permissions(egress=("a.example:443",)) and from_json(old).tcp_from_settings == ()


@pytest.mark.parametrize("text", ['{"tcp_from_settings": [{"ports": "any"}]}', '{"tcp_from_settings": [1]}', '{"tcp_from_settings": [{"setting": "h", "ports": 21}]}',
                                  '{"tcp_from_settings": [{"setting": "h", "ports": "99999"}]}', '{"tcp_from_settings": "h"}'])
def test_unreadable_tcp_permissions_are_refused(text):
    with pytest.raises(PermissionsError):
        from_json(text)


def test_the_hosts_a_tcp_setting_names_follow_its_current_value_without_its_port():
    permissions = Permissions(tcp_from_settings=(TcpAccess("host"), TcpAccess("backup")))

    assert tcp_hosts(permissions, {"host": "ftp://files.lan:2121/music", "backup": ["b.lan:21", "b.lan", "c.lan"], "ignored": "x.lan"}) == {
        "host": ("files.lan",), "backup": ("b.lan", "c.lan"),
    }
    assert tcp_hosts(permissions, {"host": "", "backup": None}) == {}
    assert tcp_hosts(NO_PERMISSIONS, {"host": "files.lan"}) == {}


def test_the_folders_the_settings_name_are_their_text_values():
    permissions = Permissions(filesystem_from_settings=(FilesystemAccess("root_dir", "rw"), FilesystemAccess("cache", "ro")))

    assert path_values(permissions, {"root_dir": "/downloads/music", "cache": "", "other": "/x"}) == {"root_dir": "/downloads/music"}
    assert path_values(permissions, {"root_dir": 5, "cache": None}) == {}


def _declaring(permissions):
    class Declared(Declaring):
        name = "declared"

    Declared.permissions = permissions
    return Declared(StaticProviderContext())
