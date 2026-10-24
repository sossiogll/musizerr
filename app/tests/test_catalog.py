"""Catalog version 2: strict parsing, compatibility, and the difference between two sets of permissions."""
from __future__ import annotations

import copy
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.catalog import (
    CATALOG_VERSION,
    CatalogError,
    LimitChange,
    PermissionsDiff,
    diff_permissions,
    parse_catalog,
    parse_entry,
    permissions_from_dict,
    permissions_to_dict,
)
from musizeer_sdk.interfaces import PROVIDER_API_VERSION
from musizeer_sdk.manifest import ROLES
from musizeer_sdk.permissions import FilesystemAccess, Permissions, ResourceLimits, TcpAccess, canonical

D = "sha256:" + "a" * 64


def entry(**kwargs):
    return {
        "name": "lrclib", "image": "ghcr.io/owner/musizeer-provider-lrclib", "digest": D,
        "provider_api_version": PROVIDER_API_VERSION, "roles": ["lyrics"], "label": "LRCLIB",
        "description": "Synced lyrics", "homepage": "https://lrclib.net",
        "permissions": {"egress": ["lrclib.net:443"], "egress_from_settings": [], "filesystem_from_settings": [], "limits": {}},
        **kwargs,
    }


def catalog(*entries, **kwargs):
    return {"name": "Mine", "version": 2, "providers": list(entries), **kwargs}


# ---- the catalog ------------------------------------------------------------------------


def test_a_catalog_lists_pinned_images_with_their_permissions():
    parsed = parse_catalog(catalog(entry()))

    assert parsed.name == "Mine" and parsed.rejected == ()
    lrclib = parsed.get("lrclib")
    assert lrclib is not None and lrclib.reference == f"ghcr.io/owner/musizeer-provider-lrclib@{D}"
    assert (lrclib.registry, lrclib.repository) == ("ghcr.io", "owner/musizeer-provider-lrclib")
    assert lrclib.roles == ("lyrics",) and lrclib.label == "LRCLIB" and lrclib.homepage == "https://lrclib.net"
    assert lrclib.permissions == Permissions(egress=("lrclib.net:443",)) and lrclib.compatible
    assert parsed.get("absent") is None


def test_an_empty_catalog_is_a_catalog():
    assert parse_catalog(catalog()).entries == ()


@pytest.mark.parametrize("document, message", [
    ("nope", "JSON object"),
    ([], "JSON object"),
    ({"name": "x", "providers": []}, "no 'version'"),
    ({"name": "x", "version": 1, "providers": []}, "version 1 is not supported .the pip-package format"),
    ({"name": "x", "version": 3, "providers": []}, "version 3 is not supported"),
    ({"name": "x", "version": "2", "providers": []}, "must be the number 2"),
    ({"name": "x", "version": True, "providers": []}, "must be the number 2"),
    ({"name": "x", "version": 2.0, "providers": []}, "must be the number 2"),
    ({"version": 2, "providers": []}, "needs a 'name'"),
    ({"name": "  ", "version": 2, "providers": []}, "needs a 'name'"),
    ({"name": "x", "version": 2}, "'providers' is a list"),
    ({"name": "x", "version": 2, "providers": {}}, "'providers' is a list"),
    ({"name": "x", "version": 2, "providers": [], "extra": 1}, "unknown fields: extra"),
])
def test_a_document_that_is_not_a_version_2_catalog_is_refused_whole_with_the_reason(document, message):
    with pytest.raises(CatalogError, match=message):
        parse_catalog(document)


def test_a_catalog_with_too_many_entries_is_refused():
    with pytest.raises(CatalogError, match="more than"):
        parse_catalog(catalog(*[entry(name=f"p{i}") for i in range(501)]))


# ---- an entry ---------------------------------------------------------------------------


@pytest.mark.parametrize("patch, message", [
    ({"name": "Bad Name"}, "not a provider name"),
    ({"name": "../x"}, "not a provider name"),
    ({"name": 3}, "not a provider name"),
    ({"image": "lrclib"}, "registry/repository"),
    ({"image": 4}, "registry/repository"),
    ({"image": "ghcr.io/x/y:latest"}, "registry/repository"),
    ({"image": f"ghcr.io/x/y@{D}"}, "its own field"),
    ({"image": "ghcr.io/Upper/Case"}, "valid repository"),
    ({"image": "gh cr.io/x"}, "registry/repository"),
    ({"digest": "latest"}, "digest"),
    ({"digest": "sha256:" + "A" * 64}, "digest"),
    ({"digest": "sha256:" + "a" * 63}, "digest"),
    ({"digest": "sha512:" + "a" * 64}, "digest"),
    ({"digest": None}, "digest"),
    ({"provider_api_version": "5"}, "provider_api_version"),
    ({"provider_api_version": True}, "provider_api_version"),
    ({"provider_api_version": 0}, "provider_api_version"),
    ({"roles": []}, "roles"),
    ({"roles": "lyrics"}, "roles"),
    ({"roles": ["lyrics", "lyrics"]}, "twice"),
    ({"roles": ["time-travel"]}, "unknown roles"),
    ({"label": ""}, "label"),
    ({"label": "x" * 101}, "label"),
    ({"description": 5}, "description"),
    ({"description": "x" * 1001}, "description"),
    ({"homepage": "javascript:alert(1)"}, "homepage"),
    ({"homepage": "ftp://x"}, "homepage"),
    ({"homepage": 5}, "homepage"),
    ({"permissions": []}, "'permissions' is an object"),
    ({"permissions": {"egress": "lrclib.net:443"}}, "'egress' is a list"),
    ({"permissions": {"egress": ["not a target"]}}, "host:port"),
    ({"permissions": {"egress": ["lrclib.net"]}}, "host:port"),
    ({"permissions": {"egress": ["lrclib.net:99999"]}}, "port"),
    ({"permissions": {"egress": ["*.com:443"]}}, "two labels"),
    ({"permissions": {"egress": ["*:443"]}}, "host"),
    ({"permissions": {"egress": ["a.*.example.com:443"]}}, "host name"),
    ({"permissions": {"egress": ["*.1.2.3.4:443"]}}, "address"),
    ({"permissions": {"tcp_from_settings": "host"}}, "'tcp_from_settings' is a list"),
    ({"permissions": {"tcp_from_settings": ["host"]}}, "objects with a 'setting' and 'ports'"),
    ({"permissions": {"tcp_from_settings": [{"setting": "host", "ports": "0"}]}}, "port"),
    ({"permissions": {"tcp_from_settings": [{"setting": "host", "ports": "any", "x": 1}]}}, "objects with a"),
    ({"permissions": {"egress_from_settings": "url"}}, "'egress_from_settings' is a list"),
    ({"permissions": {"filesystem_from_settings": ["root"]}}, "objects with a 'setting'"),
    ({"permissions": {"filesystem_from_settings": [{"setting": "root", "mode": "rwx"}]}}, "neither 'ro' nor 'rw'"),
    ({"permissions": {"limits": {"memory_mb": -1}}}, "positive integer"),
    ({"permissions": {"limits": {"memory_mb": True}}}, "positive integer"),
    ({"permissions": {"limits": {"cpu": 1}}}, "limits"),
    ({"permissions": {"network": "all"}}, "unknown fields: network"),
    ({"surprise": 1}, "unknown fields: surprise"),
])
def test_an_entry_that_is_not_valid_is_refused_with_a_reason(patch, message):
    with pytest.raises(CatalogError, match=message):
        parse_entry(entry(**patch))


@pytest.mark.parametrize("missing", ["name", "image", "digest", "provider_api_version", "roles", "label", "description", "permissions"])
def test_every_field_but_the_homepage_is_required(missing):
    raw = entry()
    del raw[missing]

    with pytest.raises(CatalogError, match=f"missing fields: {missing}"):
        parse_entry(raw)


def test_the_homepage_is_optional():
    raw = entry()
    del raw["homepage"]

    assert parse_entry(raw).homepage is None


def test_a_bad_entry_is_refused_alone_and_the_rest_of_the_catalog_is_usable():
    parsed = parse_catalog(catalog(
        entry(), entry(name="broken", digest="main"), "junk", entry(name="third"), entry(name="lrclib", label="Again"),
    ))

    assert [e.name for e in parsed.entries] == ["lrclib", "third"]
    reasons = {r.label: r.reason for r in parsed.rejected}
    assert set(reasons) == {"broken", "#2", "lrclib"}
    assert "digest" in reasons["broken"] and "JSON object" in reasons["#2"] and "more than once" in reasons["lrclib"]


def test_an_entry_for_another_contract_version_is_listed_but_not_compatible():
    other = parse_catalog(catalog(entry(provider_api_version=PROVIDER_API_VERSION + 1))).get("lrclib")
    old = parse_catalog(catalog(entry(provider_api_version=PROVIDER_API_VERSION - 1))).get("lrclib")

    assert other is not None and not other.compatible
    assert f"version {PROVIDER_API_VERSION + 1}" in other.incompatibility and f"needs version {PROVIDER_API_VERSION}" in other.incompatibility
    assert old is not None and not old.compatible
    assert parse_catalog(catalog(entry())).get("lrclib").incompatibility is None


def test_the_permissions_of_an_entry_are_canonical():
    one = parse_entry(entry(permissions={"egress": ["b.example:443", "a.example:443"]}))
    other = parse_entry(entry(permissions={"egress": ["a.example:443", "b.example:443"]}))

    assert one.permissions == other.permissions
    assert permissions_to_dict(one.permissions) == permissions_to_dict(other.permissions)


def test_the_json_form_of_permissions_round_trips():
    permissions = Permissions(
        egress=("a.example:443",), egress_from_settings=("url",),
        filesystem_from_settings=(FilesystemAccess("root_dir", "rw"),), limits=ResourceLimits(memory_mb=256, open_files=64),
    )

    assert permissions_from_dict(json.loads(json.dumps(permissions_to_dict(permissions)))) == canonical(permissions)


# ---- the difference between two sets of permissions ---------------------------------------


def test_nothing_changed_is_an_empty_diff():
    permissions = Permissions(egress=("a.example:443",))

    diff = diff_permissions(permissions, permissions, ("lyrics",), ("lyrics",))

    assert diff.is_empty and not diff.grants_more and diff == PermissionsDiff()


def test_the_diff_says_what_is_added_and_what_is_removed():
    old = Permissions(
        egress=("a.example:443", "gone.example:443"), egress_from_settings=("url",),
        filesystem_from_settings=(FilesystemAccess("root", "ro"),), limits=ResourceLimits(memory_mb=128),
    )
    new = Permissions(
        egress=("a.example:443", "new.example:443"), egress_from_settings=("host",),
        filesystem_from_settings=(FilesystemAccess("root", "rw"),), limits=ResourceLimits(memory_mb=512, processes=8),
    )

    diff = diff_permissions(old, new, ("lyrics",), ("lyrics", "metadata"))

    assert diff.egress_added == ("new.example:443",) and diff.egress_removed == ("gone.example:443",)
    assert diff.egress_from_settings_added == ("host",) and diff.egress_from_settings_removed == ("url",)
    assert diff.filesystem_added == (FilesystemAccess("root", "rw"),) and diff.filesystem_removed == (FilesystemAccess("root", "ro"),)
    assert diff.limits_changed == (LimitChange("memory_mb", 128, 512), LimitChange("processes", None, 8))
    assert diff.roles_added == ("metadata",) and diff.roles_removed == ()
    assert diff.grants_more and not diff.is_empty
    assert json.dumps(diff.to_dict())


def test_dropping_permissions_alone_does_not_ask_for_more():
    old = Permissions(egress=("a.example:443", "b.example:443"), limits=ResourceLimits(memory_mb=512))
    new = Permissions(egress=("a.example:443",), limits=ResourceLimits(memory_mb=256))

    diff = diff_permissions(old, new)

    assert diff.egress_removed == ("b.example:443",) and not diff.grants_more and not diff.is_empty


def test_a_larger_limit_or_one_back_to_the_default_asks_for_more():
    base = Permissions(limits=ResourceLimits(memory_mb=128))

    assert diff_permissions(base, Permissions(limits=ResourceLimits(memory_mb=256))).grants_more
    assert diff_permissions(base, Permissions()).grants_more
    assert not diff_permissions(base, Permissions(limits=ResourceLimits(memory_mb=64))).grants_more


_hosts = st.sampled_from([f"h{i}.example:{p}" for i in range(4) for p in (80, 443)])
_settings = st.sampled_from(["url", "host", "token", "root"])
_limit = st.one_of(st.none(), st.integers(min_value=1, max_value=4096))
_permissions = st.builds(
    Permissions,
    egress=st.lists(_hosts, max_size=5).map(tuple),
    egress_from_settings=st.lists(_settings, max_size=3).map(tuple),
    filesystem_from_settings=st.lists(st.builds(FilesystemAccess, _settings, st.sampled_from(["ro", "rw"])), max_size=3).map(tuple),
    tcp_from_settings=st.lists(st.builds(TcpAccess, _settings, st.sampled_from(["any", "21", "30000-30100"])), max_size=3).map(tuple),
    limits=st.builds(ResourceLimits, _limit, _limit, _limit),
)
_roles = st.lists(st.sampled_from(sorted(ROLES)), max_size=4, unique=True).map(tuple)


@settings(max_examples=150, deadline=None)
@given(_permissions, _permissions, _roles, _roles)
def test_the_diff_describes_exactly_how_to_get_from_one_set_to_the_other(old, new, old_roles, new_roles):
    diff = diff_permissions(old, new, old_roles, new_roles)
    old_c, new_c = canonical(old), canonical(new)

    assert (set(old_c.egress) - set(diff.egress_removed)) | set(diff.egress_added) == set(new_c.egress)
    assert (set(old_c.egress_from_settings) - set(diff.egress_from_settings_removed)) | set(diff.egress_from_settings_added) == set(new_c.egress_from_settings)
    assert (set(old_c.filesystem_from_settings) - set(diff.filesystem_removed)) | set(diff.filesystem_added) == set(new_c.filesystem_from_settings)
    assert (set(old_c.tcp_from_settings) - set(diff.tcp_removed)) | set(diff.tcp_added) == set(new_c.tcp_from_settings)
    assert (set(old_roles) - set(diff.roles_removed)) | set(diff.roles_added) == set(new_roles)
    for change in diff.limits_changed:
        assert getattr(new_c.limits, change.name) == change.new and getattr(old_c.limits, change.name) == change.old
    assert diff.is_empty == (old_c == new_c and set(old_roles) == set(new_roles))


@settings(max_examples=100, deadline=None)
@given(_permissions, _permissions)
def test_the_diff_is_symmetric_and_empty_against_itself(old, new):
    forward, backward = diff_permissions(old, new), diff_permissions(new, old)

    assert forward.egress_added == backward.egress_removed and forward.egress_removed == backward.egress_added
    assert forward.filesystem_added == backward.filesystem_removed
    assert forward.tcp_added == backward.tcp_removed and forward.tcp_removed == backward.tcp_added
    assert diff_permissions(old, old).is_empty
    assert diff_permissions(old, canonical(old)).is_empty


@settings(max_examples=100, deadline=None)
@given(_permissions)
def test_permissions_survive_the_json_form(permissions):
    assert permissions_from_dict(json.loads(json.dumps(permissions_to_dict(permissions)))) == canonical(permissions)


@settings(max_examples=100, deadline=None)
@given(st.recursive(st.none() | st.booleans() | st.integers() | st.text(max_size=10), lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=8), c, max_size=3), max_leaves=12))
def test_whatever_a_catalog_holds_it_is_a_catalog_or_a_clear_error(document):
    try:
        parse_catalog(document)
    except CatalogError:
        pass


@settings(max_examples=100, deadline=None)
@given(st.dictionaries(st.sampled_from(sorted(["name", "image", "digest", "provider_api_version", "roles", "label", "description", "homepage", "permissions", "x"])),
                       st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=12), st.lists(st.text(max_size=5), max_size=3)), max_size=10))
def test_whatever_an_entry_holds_it_is_an_entry_or_a_clear_error(raw):
    try:
        parse_entry(raw)
    except CatalogError:
        pass


def test_the_version_this_host_reads_is_two():
    assert CATALOG_VERSION == 2
    assert copy.deepcopy(catalog())["version"] == CATALOG_VERSION

